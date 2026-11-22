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

"""Node catalog for the visual flow builder.

**The server is the authority on what a node is.** This module is the single
source of truth for the node kinds a flow may contain: their ports, their
configuration fields, and the output variables they publish for
``{{node_id.output}}`` references.

Both consumers read from here, which is what keeps them from drifting:

- ``app/services/flow_graph.py`` validates a saved graph against it;
- the builder UI renders the node library panel *and* every configuration form
  from ``GET /api/v1/flows/node-catalog``, which serves this catalog plus the
  caller's live agents / integrations / assignments.

Adding a node kind is a single entry in :data:`NODE_KINDS` — no UI change is
needed for the library panel or the config drawer to pick it up.

## Port typing

Every port declares a set of *channel* types. An edge is legal when the source
port's types intersect the target port's types; the intersection also decides
which **edge kinds** (`data` / `handover` / `condition_branch` / `error`) the
connection may carry — see :data:`EDGE_KIND_BY_PORT_TYPE`.

- ``flow``     — ordinary execution/data flow, the default connection
- ``branch``   — a labelled output of a condition node
- ``error``    — the failure path every executable node exposes
- ``handover`` — an agent-to-agent handover, the platform's own primitive
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.scopes import (
    COORDINATION_SCOPE_DEFAULT,
    COORDINATION_SCOPE_OPTIONS,
    EVENT_SCOPE_FLOW,
    EVENT_SCOPE_GLOBAL,
    EVENT_SCOPE_OPTIONS,
    SUBSCRIPTION_SCOPE_ALL,
    SUBSCRIPTION_SCOPE_OPTIONS,
    SUBSCRIPTION_SCOPE_OWN,
)
from app.models.general_assistant_config import MAX_ITERATIONS_CEILING
from app.models.work_item import WORK_ITEM_STATUSES
from app.services.assignment_dispatch import MAX_CONCURRENCY_CAP
from app.services.platform_events import PLATFORM_EVENTS
from app.services.report_spec import REPORT_THEMES

# ─── Port channel types ──────────────────────────────────────────────────────

PORT_FLOW = "flow"
PORT_BRANCH = "branch"
PORT_ERROR = "error"
PORT_HANDOVER = "handover"
# The Chat start point's own channel: only the agent's `in` accepts it, so a
# chat entry can ONLY lead to an agent — the conversational endpoint the chat
# surface exposes under the FLOW's name.
PORT_CHAT = "chat"
# Data channel (a declared parameter's pin, a Value node's output): carries
# `value` edges into a consumer's `values` socket — pure wiring the runner
# never walks, plus the on-connect auto-reference (the UE data pin).
PORT_PARAM = "param"
# Capability wiring: not control flow. A capability edge never carries the
# walk — it declares "this agent also has this tool / integration / skill /
# knowledge base for this flow". One capability node may feed several agent
# nodes (the ports are `multiple`), which is what makes capabilities
# shareable. Each family is its OWN channel type so the port matrix itself
# refuses a skill plugged into the tools socket — the socket identifies what
# it accepts, no extra validation rule needed.
PORT_CAP_TOOL = "capability_tool"
PORT_CAP_INTEGRATION = "capability_integration"
PORT_CAP_SKILL = "capability_skill"
PORT_CAP_KNOWLEDGE_BASE = "capability_knowledge_base"
PORT_CAP_OUTPUT = "capability_output"
PORT_CAP_MEMORY = "capability_memory"
PORT_CAP_GUARDRAILS = "capability_guardrails"
# Wiring an LLM into a condition node turns its test from a string comparison
# into a natural-language judgement — the node's config fields switch with it.
PORT_CAP_LLM = "capability_llm"
# Coordination capabilities: one node wired to several agents gives them a
# COMMON shared-state namespace / makes them work-item peers — the sharing IS
# the wiring, visible on the canvas.
PORT_CAP_SHARED_STATE = "capability_shared_state"
PORT_CAP_ON_DELEGATE = "capability_on_delegate"
PORT_CAP_WORK_ITEMS = "capability_work_items"
# A workset is a coordination capability too, and for the same reason: one
# node wired to several agents means they build and read ONE corpus.
PORT_CAP_WORKSET = "capability_workset"
# A budget is the same sharing story applied to SPEND: one node wired
# to several agents means they run under ONE ceiling policy, and wiring it into
# a Flow node hands that policy to every agent of the flow being called.
PORT_CAP_BUDGET = "capability_budget"
# A workspace hands the agents wired to it what it gives every conversation
# filed in it - instructions, sources, bound knowledge bases, skills.
PORT_CAP_WORKSPACE = "capability_workspace"

# ─── Edge kinds (the issue's four typed edges + capability wiring) ───────────

EDGE_DATA = "data"
# Pure data wiring (a Value node or a declared parameter feeding a field):
# never walked by the runner — documentation + auto-reference, the UE data pin.
EDGE_VALUE = "value"
EDGE_HANDOVER = "handover"
EDGE_CONDITION_BRANCH = "condition_branch"
EDGE_ERROR = "error"
EDGE_CAPABILITY = "capability"

EDGE_KINDS: tuple[str, ...] = (
    EDGE_DATA,
    EDGE_HANDOVER,
    EDGE_CONDITION_BRANCH,
    EDGE_ERROR,
    EDGE_CAPABILITY,
    EDGE_VALUE,
)

# Which edge kind a shared port type authorises. The intersection of the two
# ports' type sets is mapped through this table to get the legal edge kinds for
# a connection, so an edge kind can never be asserted by the client alone.
EDGE_KIND_BY_PORT_TYPE: dict[str, str] = {
    PORT_FLOW: EDGE_DATA,
    PORT_BRANCH: EDGE_CONDITION_BRANCH,
    PORT_ERROR: EDGE_ERROR,
    PORT_HANDOVER: EDGE_HANDOVER,
    PORT_CHAT: EDGE_DATA,
    PORT_PARAM: EDGE_VALUE,
    PORT_CAP_TOOL: EDGE_CAPABILITY,
    PORT_CAP_ON_DELEGATE: EDGE_CAPABILITY,
    PORT_CAP_INTEGRATION: EDGE_CAPABILITY,
    PORT_CAP_SKILL: EDGE_CAPABILITY,
    PORT_CAP_KNOWLEDGE_BASE: EDGE_CAPABILITY,
    PORT_CAP_OUTPUT: EDGE_CAPABILITY,
    PORT_CAP_MEMORY: EDGE_CAPABILITY,
    PORT_CAP_GUARDRAILS: EDGE_CAPABILITY,
    PORT_CAP_LLM: EDGE_CAPABILITY,
    PORT_CAP_SHARED_STATE: EDGE_CAPABILITY,
    PORT_CAP_WORK_ITEMS: EDGE_CAPABILITY,
    PORT_CAP_WORKSET: EDGE_CAPABILITY,
    PORT_CAP_BUDGET: EDGE_CAPABILITY,
    PORT_CAP_WORKSPACE: EDGE_CAPABILITY,
}

# ─── Categories (the node library panel's sections, in display order) ─────────

CATEGORY_TRIGGERS = "triggers"
CATEGORY_AGENTS = "agents"
CATEGORY_CAPABILITIES = "capabilities"
CATEGORY_CONDITIONS = "conditions"
CATEGORY_ACTIONS = "actions"
CATEGORY_INTEGRATIONS = "integrations"
CATEGORY_FLOWS = "flows"
CATEGORY_UTILITIES = "utilities"
CATEGORY_OTHER = "other"

CATEGORY_ORDER: tuple[str, ...] = (
    CATEGORY_TRIGGERS,
    CATEGORY_AGENTS,
    CATEGORY_CAPABILITIES,
    CATEGORY_CONDITIONS,
    CATEGORY_ACTIONS,
    CATEGORY_INTEGRATIONS,
    CATEGORY_UTILITIES,
    CATEGORY_OTHER,
)

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_TRIGGERS: "Triggers",
    CATEGORY_AGENTS: "Agents & Flows",
    CATEGORY_CAPABILITIES: "Capabilities",
    CATEGORY_CONDITIONS: "Conditions",
    CATEGORY_ACTIONS: "Actions",
    CATEGORY_INTEGRATIONS: "Integrations",
    CATEGORY_UTILITIES: "Utilities",
    CATEGORY_OTHER: "Other",
}

# ─── Config field types the builder knows how to render ──────────────────────
#
# `variable_text` / `variable_textarea` are plain text inputs that additionally
# offer `{{` autocomplete over upstream node outputs.

FIELD_TEXT = "text"
FIELD_TEXTAREA = "textarea"
FIELD_VARIABLE_TEXT = "variable_text"
FIELD_VARIABLE_TEXTAREA = "variable_textarea"
FIELD_NUMBER = "number"
# A bounded numeric field rendered as a range slider — mirrors the agent config
# UI, where temperature and max-iterations are sliders with a fixed ceiling.
# Uses the ConfigField `min` / `max` / `step` bounds.
FIELD_SLIDER = "slider"
FIELD_BOOLEAN = "boolean"
FIELD_SELECT = "select"
FIELD_CRON = "cron"
FIELD_JSON = "json"
FIELD_KEY_VALUE = "key_value"
FIELD_MULTI_SELECT = "multi_select"

FIELD_TYPES: frozenset[str] = frozenset(
    {
        FIELD_TEXT,
        FIELD_TEXTAREA,
        FIELD_VARIABLE_TEXT,
        FIELD_VARIABLE_TEXTAREA,
        FIELD_NUMBER,
        FIELD_SLIDER,
        FIELD_BOOLEAN,
        FIELD_SELECT,
        FIELD_CRON,
        FIELD_JSON,
        FIELD_KEY_VALUE,
        FIELD_MULTI_SELECT,
    }
)

# A `select` field whose options are not static resolves them at catalog-build
# time from the caller's own visible entities.
SOURCE_AGENTS = "agents"
SOURCE_INTEGRATIONS = "integrations"
SOURCE_ASSIGNMENTS = "assignments"
SOURCE_KNOWLEDGE_BASES = "knowledge_bases"
SOURCE_FLOWS = "flows"
SOURCE_TOOLS = "tools"
SOURCE_SKILLS = "skills"
SOURCE_MODELS = "models"
# Configured LLM provider *instances* (one row per admin-configured provider).
# Distinct from SOURCE_MODELS on purpose: a model id alone is ambiguous when two
# providers publish the same one, and only the instance id can disambiguate it.
SOURCE_LLM_PROVIDERS = "llm_providers"
SOURCE_EVENT_TYPES = "event_types"
SOURCE_CUSTOM_TOOLS = "custom_tools"
SOURCE_VARIABLES = "variables"
SOURCE_RUNNERS = "runners"
# Workspaces visible to the caller — the referent of the `workspace`
# coordination scope. Dynamic because a workspace is created by users, and
# resolved per caller because a workspace is shareable.
SOURCE_WORKSPACES = "workspaces"
# Relay groups the caller may route a request through: an HTTP node
# reaching an on-premises endpoint from that group's network.
SOURCE_RELAY_GROUPS = "relay_groups"
# Fleets the caller can see: a Fleet node hands work to one's orchestrator.
SOURCE_FLEETS = "fleets"
# Report templates the caller can use (template containers of the report kind).
SOURCE_REPORT_TEMPLATES = "report_templates"

DYNAMIC_SOURCES: frozenset[str] = frozenset(
    {
        SOURCE_AGENTS,
        SOURCE_INTEGRATIONS,
        SOURCE_ASSIGNMENTS,
        SOURCE_KNOWLEDGE_BASES,
        SOURCE_FLOWS,
        SOURCE_TOOLS,
        SOURCE_SKILLS,
        SOURCE_MODELS,
        SOURCE_LLM_PROVIDERS,
        SOURCE_EVENT_TYPES,
        SOURCE_VARIABLES,
        SOURCE_CUSTOM_TOOLS,
        SOURCE_RUNNERS,
        SOURCE_WORKSPACES,
        SOURCE_RELAY_GROUPS,
        SOURCE_FLEETS,
        SOURCE_REPORT_TEMPLATES,
    }
)

# The platform-event trigger: its kind and scopes.
PLATFORM_EVENT_TRIGGER_KIND = "trigger.platform_event"
PLATFORM_EVENT_SCOPE_OWN = SUBSCRIPTION_SCOPE_OWN
PLATFORM_EVENT_SCOPE_ALL = SUBSCRIPTION_SCOPE_ALL

#: Node kinds a deployment switch hides from the catalog (``flow_access.
#: available_node_kinds``) and that refuse to run while it is off: the
#: feature they drive is not served at all.
FEATURE_GATED_NODE_KINDS: dict[str, str] = {
    "action.render_video": "VIDEO_STUDIO",
    "action.deep_investigation": "DEEP_INVESTIGATION",
}

# The import fallback kind. A LangFlow component (or a node from a newer XTM One
# version) with no local equivalent lands here with its original payload intact,
# so the graph stays legible and re-mappable instead of silently losing nodes.
KIND_UNRESOLVED = "unresolved"


# ─── Webhook concurrency ─────────────────────────────────────────────────────

#: The most runs a Webhook trigger may let its flow have active at once
#: (``max_concurrent_runs``): the ceiling an assignment's own
#: ``max_concurrent_runs`` is clamped to, one rule for both. Each run may be an
#: agent run; the cap keeps one chatty sender from starting an unbounded number.
MAX_WEBHOOK_CONCURRENT_RUNS = MAX_CONCURRENCY_CAP


def concurrent_runs_setting(raw: Any) -> int | None:
    """A Webhook trigger's ``max_concurrent_runs`` as a whole number, or None.

    Accepts an int, an integral float and a string of ASCII digits (a
    hand-written config); anything else - a fraction, a boolean, text, a
    non-ASCII digit, a string too long for ``int()`` - is None, never an
    exception. The range is not checked here: the validator reports it, the
    trigger clamps it.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None  # inf / nan are not integers
    if isinstance(raw, str):
        text = raw.strip()
        # `isdigit()` alone accepts "²" or "①", which `int()` refuses.
        if not (text.isascii() and text.isdigit()):
            return None
        try:
            return int(text)
        except ValueError:  # past the interpreter's int-from-string digit limit
            return None
    return raw if isinstance(raw, int) else None


# ─── Catalog data model ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Port:
    """One connection point on a node."""

    id: str
    label: str
    types: tuple[str, ...]
    # False = at most one edge may land here (the common case for inputs).
    multiple: bool = False


@dataclass(frozen=True)
class ConfigField:
    """One configuration input rendered in the node config drawer."""

    name: str
    label: str
    type: str
    required: bool = False
    help: str | None = None
    placeholder: str | None = None
    default: Any = None
    # Static options for a `select`, as [{"value": ..., "label": ...}].
    options: tuple[dict[str, str], ...] = ()
    # Dynamic option source for a `select` — resolved per caller.
    source: str | None = None
    # Range bounds for a `slider` (also usable to bound a `number`). None on
    # any of them lets the UI fall back to a sensible default.
    min: float | None = None
    max: float | None = None
    step: float | None = None
    # An override that can defer to the platform/agent default. A `select`
    # renders a leading "Platform default" choice that stores an empty value
    # (inherit), so an agent that carries no explicit setting reads as
    # inheriting rather than looking unset.
    inheritable: bool = False
    # Restricts the field to one of a node's operating modes. A condition node
    # is `deterministic` until an LLM is wired into it and `llm` after, and the
    # form (and the required-field check) follows that. None = always shown.
    mode: str | None = None


# A condition node's two modes — see `ConfigField.mode`.
MODE_DETERMINISTIC = "deterministic"
MODE_LLM = "llm"


@dataclass(frozen=True)
class OutputVariable:
    """A value this node publishes for ``{{node_id.<name>}}`` references.

    ``type`` is what the value is at run time when the node always publishes
    the same kind of value, else ``any``. A ``text`` value has no fields: the
    validator warns on ``{{node.<name>.KEY}}`` and the builder offers no path
    into it.
    """

    name: str
    label: str
    description: str = ""
    type: str = "any"


# The ``OutputVariable.type`` values.
OUTPUT_TEXT = "text"
OUTPUT_NUMBER = "number"
OUTPUT_BOOLEAN = "boolean"
OUTPUT_OBJECT = "object"
OUTPUT_LIST = "list"
OUTPUT_ANY = "any"


@dataclass(frozen=True)
class NodeKind:
    """The full definition of one placeable node type."""

    kind: str
    category: str
    label: str
    description: str
    # Lucide icon name — the frontend resolves it through its own icon map.
    icon: str
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()
    config_fields: tuple[ConfigField, ...] = ()
    output_variables: tuple[OutputVariable, ...] = ()
    # Free-floating annotations are not part of the executable graph.
    is_annotation: bool = False
    searchable_terms: tuple[str, ...] = field(default=())
    # Whether placing this node makes a flow depend on a model or embedding
    # provider (FR-015, FR-030).
    #
    # **The default is the opposite of every other flag on this dataclass, and
    # deliberately so.** A node type added by someone who never heard of the
    # AI-disabled mode must be held back, not run: the cost of a wrong `True`
    # is a flow that waits, the cost of a wrong `False` is a model call on a
    # deployment that forbids one. Do not "tidy" this to False to match its
    # neighbours — every exemption is declared explicitly below, one reviewable
    # line each, and `test_a_new_node_kind_defaults_to_requiring_ai` fails if
    # this default is flipped.
    requires_ai: bool = True
    # What executing this node does outside the run (#8255): the sandbox and
    # Platform Health's dry-run intercept a write and run the rest. ``None``
    # (undeclared) is a write, for the reason ``requires_ai`` defaults to
    # True: a node added without thinking about it must be held back.
    side_effect: str | None = None


#: The node acts on nothing outside the run (wiring, control flow, run
#: variables, a model judgement, a read of the platform).
SIDE_EFFECT_NONE = "none"
#: The node calls a tool by name: its effect is the tool's, as the platform's
#: read/write classifier (``services/tool_classification.py``) reads the name.
SIDE_EFFECT_TOOL = "tool"
#: The node runs an agent or a flow, whose own calls are intercepted in turn.
SIDE_EFFECT_NESTED = "nested"
#: The node changes something outside the run (the default of an undeclared one).
SIDE_EFFECT_WRITE = "write"


# ─── Shared port shapes ──────────────────────────────────────────────────────
#
# Executable nodes share the same wiring: one input accepting anything that can
# route into them, one `out` continuation, and one `error` failure path.

_IN = Port(
    id="in",
    label="In",
    types=(PORT_FLOW, PORT_BRANCH, PORT_ERROR),
    multiple=True,
)
_OUT = Port(id="out", label="Out", types=(PORT_FLOW,), multiple=True)
_ERR = Port(id="error", label="On error", types=(PORT_ERROR,), multiple=True)

# Agent nodes additionally accept and emit handovers.
_AGENT_IN = Port(
    id="in",
    label="In",
    types=(PORT_FLOW, PORT_BRANCH, PORT_ERROR, PORT_HANDOVER, PORT_CHAT),
    multiple=True,
)
_AGENT_HANDOVER_OUT = Port(
    id="handover",
    label="Handover",
    types=(PORT_HANDOVER,),
    multiple=True,
)
# Capability attachment points — one identified socket per family, rendered
# along the node's bottom edge. What lands here EXTENDS what the agent
# already carries, for this flow's runs only.
_AGENT_CAP_PORTS = (
    Port(id="tools", label="Tools", types=(PORT_CAP_TOOL,), multiple=True),
    Port(
        id="integrations",
        label="Integrations",
        types=(PORT_CAP_INTEGRATION,),
        multiple=True,
    ),
    Port(id="skills", label="Skills", types=(PORT_CAP_SKILL,), multiple=True),
    Port(
        id="knowledge_bases",
        label="Knowledge bases",
        types=(PORT_CAP_KNOWLEDGE_BASE,),
        multiple=True,
    ),
    Port(id="memory", label="Memory", types=(PORT_CAP_MEMORY,), multiple=True),
    Port(
        id="guardrails",
        label="Guardrails",
        types=(PORT_CAP_GUARDRAILS,),
        multiple=True,
    ),
    # Coordination sockets: `multiple` on the node side is what lets ONE
    # Shared state / Work items node fan out to several agents — the wiring
    # itself is the sharing contract.
    Port(
        id="shared_state",
        label="Shared state",
        types=(PORT_CAP_SHARED_STATE,),
        multiple=True,
    ),
    Port(
        id="work_items",
        label="Work items",
        types=(PORT_CAP_WORK_ITEMS,),
        multiple=True,
    ),
    Port(
        id="workset",
        label="Workset",
        types=(PORT_CAP_WORKSET,),
        multiple=True,
    ),
    # Singular in effect (a run has ONE budget) but `multiple` so one Budget
    # node fans out to every agent that should share the ceiling — the wiring
    # IS the distribution. The walker takes the first wired budget and the
    # validator warns on a second, exactly like the output schema.
    Port(
        id="budget",
        label="Budget",
        types=(PORT_CAP_BUDGET,),
        multiple=True,
    ),
    # Structured output is singular (an agent has ONE schema), but the socket
    # stays `multiple` for a uniform bottom edge — the walker takes the first
    # wired schema and the validator warns on a second.
    Port(
        id="output",
        label="Output schema",
        types=(PORT_CAP_OUTPUT,),
        multiple=True,
    ),
    Port(
        id="on_delegate",
        label="On delegate",
        types=(PORT_CAP_ON_DELEGATE,),
        multiple=True,
    ),
    # Singular in effect (a turn works in ONE workspace): the walker takes the
    # first wired node, like the budget.
    Port(
        id="workspace",
        label="Workspace",
        types=(PORT_CAP_WORKSPACE,),
        multiple=True,
    ),
)


def _cap_out(port_type: str) -> Port:
    """What a capability node emits. `multiple` is the sharing story: one
    node fans out to as many agent nodes as need it."""
    return Port(id="provides", label="Provides", types=(port_type,), multiple=True)


# An action node's workset socket. The corpus is named by the wired node, never
# by a field: the same reason the agent's tools take no workset id.
_WORKSET_IN = Port(
    id="workset", label="Workset", types=(PORT_CAP_WORKSET,), multiple=False
)

# A condition node's LLM socket. Singular in effect (the walker takes the first
# wired model), but `multiple` so one LLM node can drive several conditions.
_LLM_IN = Port(id="llm", label="LLM", types=(PORT_CAP_LLM,), multiple=True)


def _branch(port_id: str, label: str) -> Port:
    return Port(id=port_id, label=label, types=(PORT_BRANCH,), multiple=True)


# ─── The catalog ─────────────────────────────────────────────────────────────

# A work item's priority as the work-item nodes offer it; ``normal`` is stored
# as the model's ``medium``.
WORK_ITEM_PRIORITY_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "low", "label": "Low"},
    {"value": "normal", "label": "Normal"},
    {"value": "high", "label": "High"},
    {"value": "urgent", "label": "Urgent"},
)

# Blocked is not offered (#762): a blocked item needs a blocker the platform
# releases or a person's decision card, which a text field cannot carry.
WORK_ITEM_STATUS_OPTIONS: tuple[dict[str, str], ...] = tuple(
    {"value": status, "label": status.replace("_", " ").capitalize()}
    for status in WORK_ITEM_STATUSES
    if status != "blocked"
)

# What an export node publishes: the stored file and how to fetch it.
_EXPORT_OUTPUTS = (
    OutputVariable("file_id", "File id", "The exported file on the platform.", type=OUTPUT_TEXT),
    OutputVariable("filename", "File name", "The exported file's name.", type=OUTPUT_TEXT),
)

_TRIGGER_PAYLOAD = (
    OutputVariable(
        "payload",
        "Payload",
        "The trigger's raw payload — the same shape assignments receive.",
    ),
)

NODE_KINDS: tuple[NodeKind, ...] = (
    # ── Triggers ────────────────────────────────────────────────────────────
    NodeKind(
        kind="trigger.schedule",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_TRIGGERS,
        label="Schedule",
        description="Start the flow on a cron schedule.",
        icon="CalendarClock",
        requires_ai=False,
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "cron",
                "Cron expression",
                FIELD_CRON,
                required=True,
                placeholder="0 9 * * 1-5",
                help="Standard 5-field cron. The builder shows a plain-English preview.",
            ),
            ConfigField(
                "timezone",
                "Timezone",
                FIELD_TEXT,
                placeholder="Europe/Paris",
                help="Defaults to the flow owner's timezone when empty.",
            ),
        ),
        output_variables=_TRIGGER_PAYLOAD,
        searchable_terms=("cron", "timer", "recurring"),
    ),
    NodeKind(
        kind="trigger.webhook",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_TRIGGERS,
        label="Webhook",
        description="Start the flow when an external system posts to a URL.",
        icon="Webhook",
        requires_ai=False,
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "path",
                "Webhook path",
                FIELD_TEXT,
                required=True,
                placeholder="incident-created",
                help="Appended to the platform's webhook base URL.",
            ),
            ConfigField(
                "max_concurrent_runs",
                "Concurrent runs",
                FIELD_NUMBER,
                default=1,
                min=1,
                max=MAX_WEBHOOK_CONCURRENT_RUNS,
                step=1,
                help=(
                    "How many runs of this flow deliveries may have active at "
                    "once. 1 handles one delivery at a time. A delivery beyond "
                    "the limit is answered 429, and the sender retries."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("payload", "Payload", "The parsed request body."),
            OutputVariable("headers", "Headers", "The incoming request headers.", type=OUTPUT_OBJECT),
        ),
        searchable_terms=("http", "callback", "hook"),
    ),
    NodeKind(
        kind="trigger.integration_event",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_TRIGGERS,
        label="Integration event",
        description="Start the flow when a connected integration emits an event.",
        icon="Zap",
        requires_ai=False,
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "integration_id",
                "Integration",
                FIELD_SELECT,
                required=True,
                source=SOURCE_INTEGRATIONS,
            ),
            ConfigField(
                "event_type",
                "Event type",
                FIELD_TEXT,
                placeholder="issue.opened",
                help="Leave empty to react to every event from this integration.",
            ),
            ConfigField(
                "filter",
                "Event filter",
                FIELD_JSON,
                help="Optional JSON predicate applied to the event payload.",
            ),
        ),
        output_variables=(
            OutputVariable("payload", "Payload", "The event payload."),
            OutputVariable("event_type", "Event type", "The emitted event type."),
        ),
        searchable_terms=("event", "trigger", "integration"),
    ),
    NodeKind(
        kind="trigger.manual",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_TRIGGERS,
        label="Manual",
        description="Start the flow only when a user runs it by hand.",
        icon="Play",
        requires_ai=False,
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "prompt",
                "User prompt",
                FIELD_TEXTAREA,
                help=(
                    "Default message offered when the flow is run — the person "
                    "running it can edit it. Read downstream as {{node.prompt}}."
                ),
                placeholder="What should this run work on?",
            ),
        ),
        output_variables=(
            *_TRIGGER_PAYLOAD,
            OutputVariable(
                "prompt",
                "Prompt",
                "What the user asked for when starting this run.",
                type=OUTPUT_TEXT,
            ),
            OutputVariable(
                "files",
                "Files",
                "Files attached to this run — every agent node receives them.",
                type=OUTPUT_LIST,
            ),
        ),
        searchable_terms=("run", "button", "on demand", "prompt", "file", "upload"),
    ),
    NodeKind(
        kind="trigger.chat",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_TRIGGERS,
        label="Chat",
        description="Start the flow from a chat message — the flow answers as an assistant.",
        icon="MessagesSquare",
        # A chat entry leads to ONE conversational agent; the dedicated channel
        # means nothing else can even be wired.
        outputs=(Port(id="out", label="Out", types=(PORT_CHAT,), multiple=False),),
        config_fields=(
            ConfigField(
                "default_message",
                "Test message",
                FIELD_TEXTAREA,
                help=(
                    "Typed directly on the node — the message a builder Run "
                    "uses. Real conversations type their own."
                ),
                placeholder="Bonjour, peux-tu m'aider ?",
            ),
            ConfigField(
                "greeting",
                "Greeting",
                FIELD_TEXTAREA,
                help=(
                    "Optional opening line shown before the first message. "
                    "Purely cosmetic — it never reaches the agent."
                ),
                placeholder="Hi! What can I help you with?",
            ),
        ),
        output_variables=(
            OutputVariable(
                "message",
                "Message",
                "The chat message that started this run.",
                type=OUTPUT_TEXT,
            ),
            OutputVariable(
                "files",
                "Files",
                "Files attached to this run — every agent node receives them.",
                type=OUTPUT_LIST,
            ),
            *_TRIGGER_PAYLOAD,
        ),
        searchable_terms=("chat", "assistant", "conversation", "message"),
    ),
    NodeKind(
        kind="trigger.work_item",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_TRIGGERS,
        label="Work item",
        description=(
            "Start the flow when a work item is due for an agent — the run's "
            "outcome completes the item."
        ),
        icon="Inbox",
        requires_ai=False,
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "owner_agent_id",
                "Owner agent",
                FIELD_SELECT,
                required=True,
                source=SOURCE_AGENTS,
                help="The flow drains this agent's open work-item queue.",
            ),
            ConfigField(
                "correlation_prefix",
                "Correlation prefix",
                FIELD_TEXT,
                placeholder="ticket-",
                help="Only pick up items whose correlation key starts with this.",
            ),
            ConfigField(
                "priority_min",
                "Minimum priority",
                FIELD_SELECT,
                options=(
                    {"value": "low", "label": "Low"},
                    {"value": "medium", "label": "Medium"},
                    {"value": "high", "label": "High"},
                    {"value": "urgent", "label": "Urgent"},
                ),
                inheritable=True,
                help="Ignore items below this priority. Default: all.",
            ),
        ),
        output_variables=(
            OutputVariable("payload", "Work item", "The full work item."),
            OutputVariable("work_item_id", "Work item ID", "The item's id."),
            OutputVariable("title", "Title", "The item's title."),
            OutputVariable(
                "description", "Description", "The item's description."
            ),
        ),
        searchable_terms=("queue", "ticket", "task", "inbox", "backlog"),
    ),
    NodeKind(
        kind="trigger.event",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_TRIGGERS,
        label="On event",
        description=(
            "Start the flow when a declared event is emitted — every flow "
            "bound to the same event type fires (multicast)."
        ),
        icon="Radio",
        requires_ai=False,
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "event_type_id",
                "Event type",
                FIELD_SELECT,
                required=True,
                source=SOURCE_EVENT_TYPES,
                help=(
                    "The declared event signature this trigger listens to. "
                    "An Emit event node bound to the same type fires it."
                ),
            ),
            ConfigField(
                "filter",
                "Field filter",
                FIELD_JSON,
                help=(
                    "Optional JSON predicate on the event's fields. A list "
                    'means "any of": {"status": ["planned", "blocked"]}.'
                ),
            ),
            ConfigField(
                "debounce_seconds",
                "Debounce (seconds)",
                FIELD_NUMBER,
                help=(
                    "Start this flow at most once every N seconds. Events "
                    "inside the window are still recorded, they just do not "
                    "start a run. Leave empty to react to every event."
                ),
            ),
        ),
        output_variables=(
            OutputVariable(
                "fields",
                "Fields",
                "The event's typed fields — read as {{node.fields.NAME}}.",
                type=OUTPUT_OBJECT,
            ),
            OutputVariable(
                "event",
                "Event",
                "Emission metadata: {{node.event.name}}, {{node.event.id}}.",
                type=OUTPUT_OBJECT,
            ),
        ),
        searchable_terms=("event", "listener", "dispatcher", "bind", "custom"),
    ),
    NodeKind(
        kind=PLATFORM_EVENT_TRIGGER_KIND,
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_TRIGGERS,
        label="On platform event",
        description=(
            "Start the flow when the platform reports something happened - a "
            "video render ended, a work item changed, a run or background task "
            "ended - the events an assignment can subscribe to."
        ),
        icon="RadioTower",
        requires_ai=False,
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "events",
                "Events",
                FIELD_MULTI_SELECT,
                required=True,
                options=tuple(
                    {"value": name, "label": name, "description": spec["description"]}
                    for name, spec in sorted(PLATFORM_EVENTS.items())
                ),
                help="Any of these events starts a run, one run per event.",
            ),
            ConfigField(
                "filter",
                "Field filter",
                FIELD_JSON,
                help=(
                    "Optional JSON predicate on the event's payload (dotted "
                    'paths). A list means "any of": {"status": ["done"]}.'
                ),
            ),
            ConfigField(
                "scope",
                "Scope",
                FIELD_SELECT,
                default=PLATFORM_EVENT_SCOPE_OWN,
                options=SUBSCRIPTION_SCOPE_OPTIONS,
                help=(
                    "Own: an object of the flow's owner (their video, their "
                    "personal work item), or one an agent placed on this flow "
                    "takes part in. Every object is honoured only for a flow "
                    "an administrator owns."
                ),
            ),
        ),
        output_variables=(
            OutputVariable(
                "payload",
                "Payload",
                "The event's payload - {{node.payload.status}}, "
                "{{node.payload.file_id}}, {{node.payload.work_item_id}}.",
                type=OUTPUT_OBJECT,
            ),
            OutputVariable("event", "Event", "The event's name.", type=OUTPUT_TEXT),
            OutputVariable("event_id", "Event id", "The event's id.", type=OUTPUT_TEXT),
        ),
        searchable_terms=(
            "platform event", "video", "render", "work item", "run", "completed",
            "failed", "background task", "subscribe",
        ),
    ),
    # ── Agents ──────────────────────────────────────────────────────────────
    NodeKind(
        kind="agent",
        side_effect=SIDE_EFFECT_NESTED,
        category=CATEGORY_AGENTS,
        label="Agent",
        description="Run one of your agents with a prompt.",
        icon="Bot",
        inputs=(_AGENT_IN, *_AGENT_CAP_PORTS),
        outputs=(_OUT, _AGENT_HANDOVER_OUT, _ERR),
        config_fields=(
            ConfigField(
                "agent_id", "Agent", FIELD_SELECT, required=True, source=SOURCE_AGENTS
            ),
            ConfigField(
                "prompt",
                "Prompt",
                FIELD_VARIABLE_TEXTAREA,
                required=True,
                help="What the agent should do. Reference upstream outputs with {{node.field}}.",
            ),
            ConfigField(
                "persona_override",
                "Persona override",
                FIELD_TEXTAREA,
                help="Replaces the agent's own persona for this node only.",
            ),
            ConfigField(
                "model",
                "Model",
                FIELD_SELECT,
                source=SOURCE_MODELS,
                inheritable=True,
                help="Overrides the agent's LLM for this node only.",
            ),
            ConfigField(
                "temperature",
                "Temperature",
                FIELD_SLIDER,
                min=0.0,
                max=2.0,
                step=0.1,
                default=0.7,
                help="Overrides the agent's sampling temperature for this node.",
            ),
            ConfigField(
                "reasoning_effort",
                "Reasoning effort",
                FIELD_SELECT,
                options=(
                    {"value": "low", "label": "Low"},
                    {"value": "medium", "label": "Medium"},
                    {"value": "high", "label": "High"},
                    {"value": "max", "label": "Max"},
                ),
                inheritable=True,
                help="Overrides the agent's reasoning effort for this node.",
            ),
            ConfigField(
                "max_iterations",
                "Max iterations",
                FIELD_SLIDER,
                min=1,
                max=MAX_ITERATIONS_CEILING,
                step=1,
                default=25,
                help="Overrides the agent's agentic-loop cap for this node.",
            ),
            ConfigField(
                "max_tokens",
                "Max tokens",
                FIELD_NUMBER,
                help="Caps the agent's response length for this node.",
            ),
            ConfigField(
                "chat_enabled",
                "Enable chat",
                FIELD_BOOLEAN,
                help=(
                    "Let this agent answer conversationally when the flow is "
                    "started from a Chat trigger — for this flow only, the "
                    "agent's own chat setting is untouched."
                ),
            ),
            ConfigField(
                "handover_mode",
                "Handover mode",
                FIELD_SELECT,
                default="detached",
                options=(
                    {"value": "detached", "label": "Detached (parallel relay)"},
                    {"value": "wait", "label": "Wait for the relay's return"},
                ),
                help=(
                    "When the handover branch calls a flow. Detached (the "
                    "transfer spirit): the relay runs as its own parallel "
                    "life and `out` fires immediately. Wait: this run pauses "
                    "until the called flow returned."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("output", "Output", "The agent's final answer.", type=OUTPUT_TEXT),
            OutputVariable(
                "fields",
                "Fields",
                "When the agent answers in JSON (its output format, or a "
                "wired Structured output) with an object: {{node.fields.KEY}}.",
                type=OUTPUT_OBJECT,
            ),
            OutputVariable("summary", "Summary", "A short summary of the run.", type=OUTPUT_TEXT),
            OutputVariable(
                "delegations",
                "Delegations",
                "Background tasks the agent spawned (task, target, task_id).",
                type=OUTPUT_LIST,
            ),
            OutputVariable(
                "delegation_events",
                "Delegation events",
                "How many On-delegate events were emitted.",
                type=OUTPUT_NUMBER,
            ),
            OutputVariable(
                "handover",
                "Handover",
                "The last handover occurrence: {{node.handover.type}}, "
                ".target_agent_name, .question/.task/.reason, .answer.",
                type=OUTPUT_OBJECT,
            ),
            OutputVariable(
                "handovers",
                "Handovers",
                "Every handover occurrence (consult/delegate/transfer) — "
                "loop over it with For each.",
                type=OUTPUT_LIST,
            ),
            OutputVariable(
                "handover_agent_id",
                "Handover agent id",
                "The transfer target's id, when a transfer happened.",
            ),
        ),
        searchable_terms=("assistant", "llm", "ai"),
    ),
    # ── Capabilities ────────────────────────────────────────────────────────
    # Not control flow: each of these attaches something to an agent node's
    # `capabilities` port, EXTENDING what the agent already carries — for this
    # flow's runs only, never mutating the agent. The `provides` port is
    # `multiple`, so one capability node is shareable across several agents.
    # The walker never executes them as steps; it reads them when the agent
    # node runs and passes the ids through the same session_* mechanism chat
    # sessions already use.
    NodeKind(
        kind="capability.tool",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="Tool",
        description="Give the connected agents a tool — built-in or custom.",
        icon="Wrench",
        outputs=(_cap_out(PORT_CAP_TOOL),),
        config_fields=(
            ConfigField(
                "tool_ids",
                "Tools",
                FIELD_MULTI_SELECT,
                required=True,
                source=SOURCE_TOOLS,
                help="Built-in and custom tools granted to the connected agent.",
            ),
        ),
        searchable_terms=("capability", "custom tool", "function"),
    ),
    NodeKind(
        kind="capability.integration",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="Integration",
        description="Give the connected agents an integration's tools.",
        icon="Plug",
        outputs=(_cap_out(PORT_CAP_INTEGRATION),),
        config_fields=(
            ConfigField(
                "integration_ids",
                "Integrations",
                FIELD_MULTI_SELECT,
                required=True,
                source=SOURCE_INTEGRATIONS,
            ),
        ),
        searchable_terms=("capability", "connector", "binding"),
    ),
    NodeKind(
        kind="capability.skill",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="Skill",
        description="Give the connected agents a skill.",
        icon="GraduationCap",
        outputs=(_cap_out(PORT_CAP_SKILL),),
        config_fields=(
            ConfigField(
                "skill_ids",
                "Skills",
                FIELD_MULTI_SELECT,
                required=True,
                source=SOURCE_SKILLS,
            ),
        ),
        searchable_terms=("capability", "playbook", "expertise"),
    ),
    NodeKind(
        kind="capability.knowledge_base",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="Knowledge base",
        description="Give the connected agents a knowledge base to search.",
        icon="BookOpen",
        outputs=(_cap_out(PORT_CAP_KNOWLEDGE_BASE),),
        config_fields=(
            ConfigField(
                "knowledge_base_ids",
                "Knowledge bases",
                FIELD_MULTI_SELECT,
                required=True,
                source=SOURCE_KNOWLEDGE_BASES,
            ),
        ),
        searchable_terms=("capability", "kb", "rag", "documents"),
    ),
    NodeKind(
        kind="capability.structured_output",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="Structured output",
        description="Make the connected agent return JSON matching a schema.",
        icon="Braces",
        outputs=(_cap_out(PORT_CAP_OUTPUT),),
        config_fields=(
            ConfigField(
                "schema",
                "JSON Schema",
                FIELD_JSON,
                required=True,
                help=(
                    "A JSON Schema the agent's answer must match. Sets the "
                    "agent to JSON output for this run — the same property the "
                    "agent carries natively."
                ),
                placeholder='{"type": "object", "properties": {}}',
            ),
        ),
        searchable_terms=("capability", "json", "schema", "format", "output"),
    ),
    NodeKind(
        kind="capability.memory",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="Memory",
        description="Set which memory tiers the connected agent uses.",
        icon="Brain",
        outputs=(_cap_out(PORT_CAP_MEMORY),),
        config_fields=(
            ConfigField(
                "short_term",
                "Short-term",
                FIELD_BOOLEAN,
                help="In-conversation working memory.",
            ),
            ConfigField(
                "medium_term",
                "Medium-term",
                FIELD_BOOLEAN,
                help="Recent-conversations recall.",
            ),
            ConfigField(
                "persistent",
                "Persistent",
                FIELD_BOOLEAN,
                help="Long-term agent memory across runs.",
            ),
            ConfigField(
                "user",
                "User memory",
                FIELD_BOOLEAN,
                help="Per-user memory (identified users only).",
            ),
        ),
        searchable_terms=("capability", "memory", "recall", "context"),
    ),
    NodeKind(
        kind="capability.guardrails",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="Guardrails",
        description="Inject safety constraints into the connected agent.",
        icon="ShieldCheck",
        outputs=(_cap_out(PORT_CAP_GUARDRAILS),),
        config_fields=(
            ConfigField(
                "guardrails",
                "Guardrails",
                FIELD_TEXTAREA,
                required=True,
                help=(
                    "Safety constraints injected into the agent's system "
                    "prompt, separate from its persona."
                ),
            ),
        ),
        searchable_terms=("capability", "safety", "constraints", "policy"),
    ),
    NodeKind(
        kind="capability.llm",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="LLM",
        description=(
            "Let a model decide — wire it into a condition to judge in plain "
            "language, or into Call tool to pick the tool and its arguments."
        ),
        icon="Sparkles",
        outputs=(_cap_out(PORT_CAP_LLM),),
        config_fields=(
            ConfigField(
                "provider",
                "Provider",
                FIELD_SELECT,
                source=SOURCE_LLM_PROVIDERS,
                inheritable=True,
                help=(
                    "Which configured provider serves the call. Pin one when "
                    "several publish the same model id — the model name alone "
                    "cannot tell them apart. Defaults to the platform's own "
                    "resolution."
                ),
            ),
            ConfigField(
                "model",
                "Model",
                FIELD_SELECT,
                source=SOURCE_MODELS,
                inheritable=True,
                help="Which model judges. Defaults to the platform's routing model.",
            ),
            ConfigField(
                "temperature",
                "Temperature",
                FIELD_SLIDER,
                min=0.0,
                max=2.0,
                step=0.1,
                inheritable=True,
                help=(
                    "Lower is more repeatable — worth pinning low for a "
                    "decision. Left empty, the provider's own default applies."
                ),
            ),
            ConfigField(
                "max_tokens",
                "Max tokens",
                FIELD_NUMBER,
                min=1,
                inheritable=True,
                help=(
                    "Caps the decision's length. A yes/no judgement needs a "
                    "handful; picking a tool's arguments needs room for the "
                    "argument object."
                ),
            ),
            ConfigField(
                "reasoning_effort",
                "Reasoning effort",
                FIELD_SELECT,
                options=(
                    {"value": "low", "label": "Low"},
                    {"value": "medium", "label": "Medium"},
                    {"value": "high", "label": "High"},
                    {"value": "max", "label": "Max"},
                ),
                inheritable=True,
                help="Only honoured by models that expose it.",
            ),
            ConfigField(
                "instructions",
                "Instructions",
                FIELD_TEXTAREA,
                help=(
                    "Optional guidance added to the judgement, e.g. how strict "
                    "to be or what to weigh."
                ),
            ),
        ),
        searchable_terms=(
            "capability", "llm", "model", "ai", "judge", "classify", "provider",
        ),
    ),
    NodeKind(
        kind="capability.on_delegate",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="On delegate",
        description=(
            "Emit a declared event when the wired agent delegates a "
            "background task — at spawn (the flow-visible signal of "
            "chat-level delegation) and/or at completion (the event carries "
            "the delegated RESULT). Listener flows pick it up as their own "
            "runs; join in-flow with Wait for event."
        ),
        icon="RadioTower",
        outputs=(_cap_out(PORT_CAP_ON_DELEGATE),),
        config_fields=(
            ConfigField(
                "event_type_id",
                "Event type",
                FIELD_SELECT,
                required=True,
                source=SOURCE_EVENT_TYPES,
                help=(
                    "Fields can reference {{delegation.task}}, "
                    "{{delegation.target_agent_name}}, "
                    "{{delegation.target_agent_id}} and {{delegation.task_id}} "
                    "— plus, on completion, {{delegation.result}} and "
                    "{{delegation.status}}."
                ),
            ),
            ConfigField(
                "emit_on",
                "Emit on",
                FIELD_SELECT,
                required=True,
                default="spawn",
                options=(
                    {"value": "spawn", "label": "Spawn (task info)"},
                    {"value": "completion", "label": "Completion (result)"},
                    {"value": "both", "label": "Both"},
                ),
                help=(
                    "Spawn fires immediately with the task; completion fires "
                    "when the delegated run ends, carrying "
                    "{{delegation.result}} and {{delegation.status}} "
                    "(completed / failed / cancelled / incomplete). "
                    "Completion mappings "
                    "may only reference {{delegation.*}} — the run has moved "
                    "on by then."
                ),
            ),
            ConfigField(
                "scope",
                "Scope",
                FIELD_SELECT,
                required=True,
                default=EVENT_SCOPE_GLOBAL,
                options=EVENT_SCOPE_OPTIONS,
                help=(
                    "Global reaches every flow listening to the type — the "
                    "usual choice for a delegation handler. Pick THIS RUN "
                    "for a completion join (Wait for event downstream): "
                    "concurrent runs stay isolated."
                ),
            ),
        ),
        searchable_terms=(
            "capability", "delegate", "delegation", "event", "background",
            "handover", "signal",
        ),
    ),
    NodeKind(
        kind="capability.shared_state",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="Shared state",
        description=(
            "A common key→value blackboard for every agent wired to it — "
            "read/write state that outlives each agent's own step."
        ),
        icon="Database",
        outputs=(_cap_out(PORT_CAP_SHARED_STATE),),
        config_fields=(
            ConfigField(
                "scope",
                "Scope",
                FIELD_SELECT,
                required=True,
                default=COORDINATION_SCOPE_DEFAULT,
                options=COORDINATION_SCOPE_OPTIONS,
                help=(
                    "This run: each execution gets an isolated blackboard — no "
                    "collisions between concurrent runs. This flow: state "
                    "persists across the flow's runs. Global: raw keys, shared "
                    "with everything else the owner runs."
                ),
            ),
            ConfigField(
                "workspace_id",
                "Workspace",
                FIELD_SELECT,
                source=SOURCE_WORKSPACES,
                help=(
                    "Only used when Scope is A WORKSPACE. The run fails with "
                    "a clear error rather than falling back to Global if this "
                    "is empty or the workspace is archived — an isolated "
                    "namespace collapsing into the shared one would be a leak, "
                    "not a lesser service."
                ),
            ),
        ),
        searchable_terms=(
            "capability", "state", "blackboard", "memory", "coordination", "share"
        ),
    ),
    NodeKind(
        kind="capability.workspace",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="Workspace knowledge",
        description=(
            "What a workspace gives every conversation filed in it - its "
            "instructions, sources, bound knowledge bases and skills - for the "
            "agents wired to it."
        ),
        icon="FolderOpen",
        outputs=(_cap_out(PORT_CAP_WORKSPACE),),
        config_fields=(
            ConfigField(
                "workspace_id",
                "Workspace",
                FIELD_SELECT,
                source=SOURCE_WORKSPACES,
                required=True,
                help=(
                    "The run fails with a clear error if the workspace is "
                    "missing, archived or not visible to the flow's owner - "
                    "never runs without the knowledge it was wired for."
                ),
            ),
        ),
        searchable_terms=(
            "capability", "workspace", "project", "sources", "instructions",
            "knowledge", "skills",
        ),
    ),
    NodeKind(
        kind="capability.work_items",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="Work items",
        description=(
            "A durable task queue between the agents wired to it — they can "
            "create, claim and complete work items for each other."
        ),
        icon="ListChecks",
        outputs=(_cap_out(PORT_CAP_WORK_ITEMS),),
        config_fields=(
            ConfigField(
                "peer_agent_ids",
                "Additional peers",
                FIELD_MULTI_SELECT,
                source=SOURCE_AGENTS,
                help=(
                    "Agents wired to this node are already each other's "
                    "peers; add agents outside the flow here."
                ),
            ),
            # A workspace, and NO `scope` field — deliberately unlike the
            # shared-state and workset nodes. Those namespace a KEY,
            # so `run` and `flow` mean something for them. A work item has no
            # key: it is addressed by id, owner and correlation key. The only
            # scopes it can express are "a workspace" and "not scoped", so a
            # three-value select would offer two choices that do nothing.
            ConfigField(
                "workspace_id",
                "Workspace",
                FIELD_SELECT,
                source=SOURCE_WORKSPACES,
                help=(
                    "File this queue's items into a workspace, so every agent "
                    "pointed at it shares one backlog. Left empty, items stay "
                    "visible to their author alone as they are today."
                ),
            ),
        ),
        searchable_terms=(
            "capability", "queue", "task", "ticket", "coordination", "delegate",
            "workspace",
        ),
    ),
    NodeKind(
        kind="capability.workset",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="Workset",
        description=(
            "A large working corpus the agents wired to it build and query "
            "together — collect many documents, summarise and rank each, then "
            "reason over the whole set without ever holding it in a context."
        ),
        icon="Library",
        inputs=(_LLM_IN,),
        outputs=(_cap_out(PORT_CAP_WORKSET),),
        config_fields=(
            ConfigField(
                "key",
                "Name",
                FIELD_TEXT,
                required=True,
                default="reports",
                placeholder="reports",
                help=(
                    "Identifies the corpus within its scope. Reuse the same "
                    "name across runs of a recurring job so a sliding window "
                    "picks up where it left off instead of starting cold."
                ),
            ),
            ConfigField(
                "scope",
                "Scope",
                FIELD_SELECT,
                required=True,
                default=COORDINATION_SCOPE_DEFAULT,
                options=COORDINATION_SCOPE_OPTIONS,
                help=(
                    "This run: each execution gets its own corpus, so "
                    "concurrent runs never mix. THIS FLOW is what a recurring "
                    "report wants — the corpus survives between runs, which is "
                    "what makes re-reading a window cheaper than rebuilding it."
                ),
            ),
            ConfigField(
                "workspace_id",
                "Workspace",
                FIELD_SELECT,
                source=SOURCE_WORKSPACES,
                help=(
                    "Only used when Scope is A WORKSPACE. The run fails with "
                    "a clear error rather than falling back to Global if this "
                    "is empty or the workspace is archived — an isolated "
                    "namespace collapsing into the shared one would be a leak, "
                    "not a lesser service."
                ),
            ),
            ConfigField(
                "processing",
                "Summaries and priorities",
                FIELD_SELECT,
                required=True,
                default="auto",
                options=(
                    {"value": "auto", "label": "Automatic (wire an LLM in)"},
                    {"value": "agent", "label": "A processor agent drains the queue"},
                    {"value": "none", "label": "The reading agent does it itself"},
                ),
                help=(
                    "Automatic keeps per-item work out of the reading agent's "
                    "loop, which is the difference between a 200-report job "
                    "finishing and exhausting its iteration budget."
                ),
            ),
            ConfigField(
                "processor_agent_id",
                "Processor agent",
                FIELD_SELECT,
                source=SOURCE_AGENTS,
                help=(
                    "Only for the processor-agent mode: the agent that claims "
                    "items and summarises them, one at a time."
                ),
            ),
            ConfigField(
                "workers",
                "Parallel workers",
                FIELD_SLIDER,
                min=1,
                max=8,
                step=1,
                default=1,
                help=(
                    "How many items are processed at once. Also the divisor in "
                    "the ETA the agents read."
                ),
            ),
            ConfigField(
                "max_items",
                "Max items",
                FIELD_NUMBER,
                min=1,
                default=500,
                help=(
                    "Past this, the lowest-priority items lose their content to "
                    "make room. Their summaries stay, and every read reports "
                    "how many were evicted."
                ),
            ),
            ConfigField(
                "max_content_chars",
                "Max total characters",
                FIELD_NUMBER,
                min=1000,
                default=20000000,
                help="The other eviction bound: total stored text.",
            ),
            ConfigField(
                "max_auto_calls",
                "Max automatic summaries",
                FIELD_NUMBER,
                min=0,
                default=200,
                help=(
                    "The spend ceiling for the automatic mode: how many "
                    "summarisation calls this corpus may ever pay for. Past "
                    "it, items are left for an agent to summarise and every "
                    "read says so. Set 0 to pay for none."
                ),
            ),
            ConfigField(
                "retention_hours",
                "Keep for (hours)",
                FIELD_NUMBER,
                min=1,
                help=(
                    "Optional: purge the corpus this long after it is created. "
                    "A workset is a workspace, not a deliverable."
                ),
            ),
        ),
        searchable_terms=(
            "capability", "corpus", "workset", "reports", "summarise",
            "synthesis", "coordination", "rag",
        ),
    ),
    NodeKind(
        kind="capability.budget",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CAPABILITIES,
        label="Budget",
        description=(
            "Cap what the agents wired to it may spend on one run — money, "
            "tokens, tool calls or time — and optionally finish on a cheaper "
            "model instead of stopping."
        ),
        icon="Wallet",
        outputs=(_cap_out(PORT_CAP_BUDGET),),
        config_fields=(
            ConfigField(
                "max_cost_usd",
                "Max cost (USD)",
                FIELD_NUMBER,
                min=0,
                step=0.01,
                placeholder="Inherits the platform budget",
                inheritable=True,
                help=(
                    "Hard ceiling on the priced model spend of one run. Needs "
                    "pricing for the model; with none known, cost is not "
                    "enforced and the other limits still apply. Empty "
                    "inherits the platform budget."
                ),
            ),
            ConfigField(
                "max_total_tokens",
                "Max tokens",
                FIELD_NUMBER,
                min=0,
                placeholder="Inherits the platform budget",
                inheritable=True,
                help=(
                    "Hard ceiling on the tokens one run may consume. Empty "
                    "inherits the platform budget."
                ),
            ),
            ConfigField(
                "max_tool_calls",
                "Max tool calls",
                FIELD_NUMBER,
                min=0,
                placeholder="Inherits the platform budget",
                inheritable=True,
                help=(
                    "Bounds parallel tool use, which Max iterations cannot: "
                    "one iteration can fan out into many calls."
                ),
            ),
            ConfigField(
                "max_duration_seconds",
                "Max duration (seconds)",
                FIELD_NUMBER,
                min=0,
                placeholder="Inherits the platform budget",
                inheritable=True,
                help=(
                    "Wall-clock ceiling for one run. Combined with the run's "
                    "own deadline — the tighter of the two applies."
                ),
            ),
            ConfigField(
                "fallback_enabled",
                "Finish on a cheaper model",
                FIELD_BOOLEAN,
                help=(
                    "When the COST ceiling can no longer pay for the next "
                    "call, continue on the fallback model instead of "
                    "stopping. Once per run, cost only — a cheaper model buys "
                    "no extra tokens, tool calls or time."
                ),
            ),
            ConfigField(
                "fallback_model",
                "Fallback model",
                FIELD_SELECT,
                source=SOURCE_MODELS,
                inheritable=True,
                help=(
                    "Which cheaper model to finish on. Empty uses the "
                    "platform fallback model (Settings → AI and models → Models)."
                ),
            ),
            ConfigField(
                "final_answer_reserve",
                "Final-answer reserve",
                FIELD_SLIDER,
                min=0.0,
                max=0.5,
                step=0.05,
                inheritable=True,
                help=(
                    "Share of the cost and token ceilings held back so an "
                    "exhausted run can still write its answer instead of "
                    "returning a raw digest of tool output."
                ),
            ),
        ),
        searchable_terms=(
            "capability", "budget", "cost", "spend", "money", "usd", "cap",
            "ceiling", "limit", "tokens", "quota", "failsafe", "fallback",
        ),
    ),
    # ── Conditions ──────────────────────────────────────────────────────────
    NodeKind(
        kind="condition.if",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CONDITIONS,
        label="If / Else",
        description=(
            "Route down one of two branches on a condition — a string test, or "
            "a plain-language question once an LLM is wired in."
        ),
        icon="GitBranch",
        requires_ai=False,
        inputs=(_IN, _LLM_IN),
        outputs=(_branch("true", "True"), _branch("false", "False")),
        config_fields=(
            ConfigField(
                "left",
                "Value",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="{{node.output}}",
                mode=MODE_DETERMINISTIC,
            ),
            ConfigField(
                "operator",
                "Operator",
                FIELD_SELECT,
                required=True,
                default="equals",
                options=(
                    {"value": "equals", "label": "equals"},
                    {"value": "not_equals", "label": "does not equal"},
                    {"value": "contains", "label": "contains"},
                    {"value": "not_contains", "label": "does not contain"},
                    {"value": "matches", "label": "matches regex"},
                    {"value": "gt", "label": "is greater than"},
                    {"value": "lt", "label": "is less than"},
                    {"value": "is_empty", "label": "is empty"},
                    {"value": "is_not_empty", "label": "is not empty"},
                ),
                mode=MODE_DETERMINISTIC,
            ),
            ConfigField(
                "right",
                "Compared to",
                FIELD_VARIABLE_TEXT,
                mode=MODE_DETERMINISTIC,
            ),
            ConfigField(
                "question",
                "Question",
                FIELD_VARIABLE_TEXTAREA,
                required=True,
                mode=MODE_LLM,
                placeholder="Is {{node.output}} reporting a critical incident?",
                help=(
                    "A yes/no question the model answers about the values you "
                    "reference. Yes takes the True branch."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("result", "Result", "True when the condition held.", type=OUTPUT_BOOLEAN),
            OutputVariable(
                "reason",
                "Reason",
                "Why the model answered as it did (LLM mode only).",
            ),
        ),
        searchable_terms=("branch", "condition", "else", "llm"),
    ),
    NodeKind(
        kind="condition.switch",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CONDITIONS,
        label="Switch",
        description=(
            "Route down one of several branches — by exact match, or by having "
            "an LLM pick the branch that fits."
        ),
        icon="Split",
        requires_ai=False,
        inputs=(_IN, _LLM_IN),
        # Cases are user-defined; `case` carries every matched branch (the edge
        # label names the case) and `default` is the fallthrough.
        outputs=(_branch("case", "Case"), _branch("default", "Default")),
        config_fields=(
            ConfigField(
                "value",
                "Value",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="{{node.output}}",
                help="What to route on.",
            ),
            # ONE list for both modes — wiring an LLM must never hide the cases
            # you typed (they are the branches; the sockets are minted from
            # them). Only the matching semantics change with the mode.
            ConfigField(
                "cases",
                "Cases",
                FIELD_KEY_VALUE,
                required=True,
                help=(
                    "One entry per branch: the case name and the value it "
                    "matches — or, with an LLM wired in, a plain-language "
                    "description of what belongs in it."
                ),
            ),
        ),
        output_variables=(
            OutputVariable(
                "matched", "Matched case", "The case that matched, if any."
            ),
            OutputVariable(
                "reason",
                "Reason",
                "Why the model chose that case (LLM mode only).",
            ),
        ),
        searchable_terms=("case", "route", "multi branch", "llm"),
    ),
    NodeKind(
        kind="condition.classify",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_CONDITIONS,
        label="Classify (CLM)",
        description=(
            "Let a classifier language model (Jev, Clef, ...) pick a label for "
            "the content and route down that label's branch — a fast, cheap "
            "filter in front of an agent."
        ),
        icon="Tags",
        # A model call: the default, stated for the reader.
        requires_ai=True,
        inputs=(_IN,),
        # One socket per configured label (`case:<label>`, minted from
        # `cases` exactly like a Switch); `default` takes what the model is
        # not confident enough about; `error` takes a call that produced no
        # decision, so a provider outage never silently drops content.
        outputs=(
            _branch("case", "Label"),
            _branch("default", "Uncertain"),
            _ERR,
        ),
        config_fields=(
            ConfigField(
                "provider",
                "Provider",
                FIELD_SELECT,
                source=SOURCE_LLM_PROVIDERS,
                inheritable=True,
                help=(
                    "The configured provider serving the decision model "
                    "(OpenRouter, OpenAI, Ollama). Defaults to the one that lists the "
                    "model, or the only one available to you that serves "
                    "decision models."
                ),
            ),
            ConfigField(
                "model",
                "Decision model",
                FIELD_TEXT,
                required=True,
                placeholder="typesafe/jev-1.13",
                help=(
                    "The classifier's id as the provider names it, e.g. "
                    "typesafe/jev-1.13 on OpenRouter."
                ),
            ),
            ConfigField(
                "input",
                "Content",
                FIELD_VARIABLE_TEXTAREA,
                required=True,
                placeholder="{{node.output}}",
                help="What the model classifies, e.g. the alert an upstream node produced.",
            ),
            ConfigField(
                "question",
                "Question",
                FIELD_VARIABLE_TEXTAREA,
                required=True,
                placeholder="Does this alert need a SOC analyst?",
                help="The decision to make about the content.",
            ),
            ConfigField(
                "cases",
                "Labels",
                FIELD_KEY_VALUE,
                required=True,
                help=(
                    "One entry per branch: the label and a plain-language "
                    "description of what belongs in it. Two labels make a "
                    "yes/no filter."
                ),
            ),
            ConfigField(
                "min_confidence",
                "Confidence floor",
                FIELD_SLIDER,
                min=0.0,
                max=1.0,
                step=0.05,
                default=0.0,
                help=(
                    "Below this confidence the content takes the Uncertain "
                    "branch instead of its label's — route it to a person or "
                    "to the agent rather than drop it."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("label", "Label", "The label the model picked."),
            OutputVariable(
                "confidence", "Confidence", "How sure the model is, from 0 to 1."
            ),
            OutputVariable(
                "probabilities",
                "Probabilities",
                "The probability of every label: {{node.probabilities.LABEL}}.",
            ),
        ),
        searchable_terms=(
            "clm", "classifier", "classify", "decision model", "jev", "clef",
            "triage", "filter", "label",
        ),
    ),
    # ── Actions ─────────────────────────────────────────────────────────────
    NodeKind(
        kind="action.send_message",
        category=CATEGORY_ACTIONS,
        label="Send message",
        description="Post a message to a channel.",
        icon="Send",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "channel",
                "Channel",
                FIELD_SELECT,
                required=True,
                options=(
                    {"value": "slack", "label": "Slack"},
                    {"value": "teams", "label": "Microsoft Teams"},
                    {"value": "discord", "label": "Discord"},
                    {"value": "telegram", "label": "Telegram"},
                    {"value": "whatsapp", "label": "WhatsApp"},
                    {"value": "google_chat", "label": "Google Chat"},
                    {"value": "email", "label": "Email"},
                ),
            ),
            ConfigField("target", "Recipient", FIELD_VARIABLE_TEXT, required=True),
            ConfigField("message", "Message", FIELD_VARIABLE_TEXTAREA, required=True),
        ),
        output_variables=(
            OutputVariable("result", "Result", "The delivery result."),
        ),
        searchable_terms=("notify", "slack", "email", "post"),
    ),
    NodeKind(
        kind="action.notify",
        category=CATEGORY_ACTIONS,
        label="Notify",
        description=(
            "Send a notification to the platform's notification bell — "
            "durable (activity feed) and pushed live."
        ),
        icon="Bell",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "title",
                "Title",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="Triage terminé : {{ag1.output}}",
            ),
            ConfigField(
                "message",
                "Message",
                FIELD_VARIABLE_TEXTAREA,
                help="Optional detail line shown under the title.",
            ),
        ),
        output_variables=(
            OutputVariable("delivered", "Delivered", "True once the bell rang.", type=OUTPUT_BOOLEAN),
        ),
        searchable_terms=("notification", "bell", "alert", "toast", "notify"),
    ),
    NodeKind(
        kind="action.ask_user",
        category=CATEGORY_ACTIONS,
        label="Ask user",
        description=(
            "Pause the run and ask a human — the question lands in the "
            "notification bell, and their answer flows out of this node."
        ),
        icon="MessageCircleQuestion",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "title",
                "Title",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="Validation requise",
            ),
            ConfigField(
                "question",
                "Question",
                FIELD_VARIABLE_TEXTAREA,
                required=True,
                placeholder="Approuves-tu la publication de : {{ag1.output}} ?",
                help="Shown to the user; the run stays paused until they answer.",
            ),
            ConfigField(
                "options",
                "Options",
                FIELD_JSON,
                placeholder='[{"key": "A", "label": "Publish", "effect": "The post goes live"}]',
                help="Optional choices shown as buttons; leave empty for a free-text answer.",
            ),
            ConfigField(
                "recommendation",
                "Recommended option",
                FIELD_TEXT,
                placeholder="A",
                help="Optional key of the option the card marks as recommended.",
            ),
            ConfigField(
                "deadline",
                "Deadline",
                FIELD_VARIABLE_TEXT,
                placeholder="2026-12-01T17:00:00Z",
                help="Optional ISO-8601 date and time shown on the card.",
            ),
        ),
        output_variables=(
            OutputVariable("response", "Response", "The answer the user typed.", type=OUTPUT_TEXT),
            OutputVariable("option", "Option", "The key of the option chosen, empty for free text.", type=OUTPUT_TEXT),
            OutputVariable("responded_by", "Responded by", "Who answered the question."),
        ),
        searchable_terms=(
            "human", "hitl", "question", "approval", "input", "answer", "ask",
        ),
    ),
    NodeKind(
        kind="action.emit_event",
        category=CATEGORY_ACTIONS,
        label="Emit event",
        description=(
            "Fire a declared event — every On event trigger and Wait for "
            "event node bound to the same type hears it."
        ),
        icon="RadioTower",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "event_type_id",
                "Event type",
                FIELD_SELECT,
                required=True,
                source=SOURCE_EVENT_TYPES,
                help="The declared signature this emission must satisfy.",
            ),
            ConfigField(
                "scope",
                "Scope",
                FIELD_SELECT,
                default=EVENT_SCOPE_FLOW,
                options=EVENT_SCOPE_OPTIONS,
                help=(
                    "How far the event carries: waiters in this run only, "
                    "any run of this flow, or every active flow you own."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("event_id", "Event ID", "The emission's id."),
            OutputVariable(
                "listeners_started",
                "Listeners started",
                "How many flow runs this emission started.",
            ),
        ),
        searchable_terms=("event", "dispatch", "fire", "broadcast", "signal"),
    ),
    NodeKind(
        kind="action.create_work_item",
        category=CATEGORY_ACTIONS,
        label="Create work item",
        description="Open a durable work item for an agent to pick up.",
        icon="ListChecks",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField("title", "Title", FIELD_VARIABLE_TEXT, required=True),
            ConfigField("body", "Description", FIELD_VARIABLE_TEXTAREA),
            ConfigField(
                "priority",
                "Priority",
                FIELD_SELECT,
                default="normal",
                options=WORK_ITEM_PRIORITY_OPTIONS,
            ),
            ConfigField(
                "owner_agent_id", "Owner agent", FIELD_SELECT, source=SOURCE_AGENTS
            ),
            ConfigField(
                "correlation_key",
                "Correlation key",
                FIELD_VARIABLE_TEXT,
                help=(
                    "Deduplicates: if an open item already carries this key, "
                    "it is reused instead of minting a duplicate."
                ),
            ),
            ConfigField(
                "wait",
                "Wait for completion",
                FIELD_BOOLEAN,
                help=(
                    "Park the run until an agent completes the item; its "
                    "result is then published as {{node.result}}."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("work_item_id", "Work item ID", "The created work item's id."),
            OutputVariable(
                "result",
                "Result",
                "The completed item's result (when waiting).",
            ),
        ),
        searchable_terms=("task", "todo", "queue"),
    ),
    NodeKind(
        kind="action.trigger_assignment",
        category=CATEGORY_ACTIONS,
        label="Trigger assignment",
        description="Run an existing assignment and wait for its result.",
        icon="Workflow",
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "assignment_id",
                "Assignment",
                FIELD_SELECT,
                required=True,
                source=SOURCE_ASSIGNMENTS,
            ),
            ConfigField(
                "payload",
                "Trigger payload",
                FIELD_JSON,
                help="Merged into the assignment's trigger payload.",
            ),
        ),
        output_variables=(
            OutputVariable("result", "Result", "The assignment run's result."),
            OutputVariable("run_id", "Run ID", "The created assignment run's id."),
        ),
        searchable_terms=("assignment", "automation", "run"),
    ),
    NodeKind(
        kind="action.call_tool",
        side_effect=SIDE_EFFECT_TOOL,
        category=CATEGORY_ACTIONS,
        label="Call tool",
        description=(
            "Invoke a tool directly — flow-callable built-ins and your "
            "custom tools, with a typed form generated from the tool's "
            "signature. Wire an LLM in and the model picks which of the "
            "tools you offer to call, and with what arguments."
        ),
        icon="Wrench",
        inputs=(_IN, _LLM_IN),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "tool_name",
                "Tool",
                FIELD_TEXT,
                required=True,
                mode=MODE_DETERMINISTIC,
            ),
            # The pins carry a source so that a write holds an id it adds to
            # the tools its writer can see (`check_source_references`).
            ConfigField(
                "tool_id",
                "Picked tool",
                FIELD_SELECT,
                source=SOURCE_CUSTOM_TOOLS,
                mode=MODE_DETERMINISTIC,
                help=(
                    "Id of the custom tool picked for `tool_name`. It runs while "
                    "it is still an enabled tool of that name this flow can "
                    "use; otherwise the name is resolved."
                ),
            ),
            ConfigField(
                "arguments",
                "Arguments",
                FIELD_JSON,
                mode=MODE_DETERMINISTIC,
            ),
            ConfigField(
                "objective",
                "Objective",
                FIELD_VARIABLE_TEXTAREA,
                required=True,
                mode=MODE_LLM,
                placeholder="Summarise {{node.output}} and store it as a work item",
                help=(
                    "What this step must achieve. The model picks the tool "
                    "that gets there and fills its arguments from the tool's "
                    "own signature."
                ),
            ),
            ConfigField(
                "candidate_tools",
                "Tools the model may choose from",
                FIELD_MULTI_SELECT,
                required=True,
                mode=MODE_LLM,
                help=(
                    "The model may only call one of these. There is "
                    "deliberately no 'any tool' option — the reach of this "
                    "node stays what you declared."
                ),
            ),
            ConfigField(
                "candidate_tool_ids",
                "Picked candidate tools",
                FIELD_JSON,
                source=SOURCE_CUSTOM_TOOLS,
                mode=MODE_LLM,
                help=(
                    "Candidate name -> id of the custom tool picked for it. Each "
                    "is offered while it is still an enabled tool of that name "
                    "this flow can use; otherwise the name is resolved."
                ),
            ),
            ConfigField(
                "argument_hints",
                "Argument guidance",
                FIELD_VARIABLE_TEXTAREA,
                mode=MODE_LLM,
                help=(
                    "Optional guidance on how to fill the arguments — units, "
                    "formats, which upstream value belongs where."
                ),
            ),
            ConfigField(
                "max_attempts",
                "Max attempts",
                FIELD_NUMBER,
                default=2,
                min=1,
                max=5,
                mode=MODE_LLM,
                help=(
                    "How many times the model may re-answer when the "
                    "arguments it produced do not satisfy the tool's schema."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("result", "Result", "The tool's return value."),
            OutputVariable(
                "selected_tool",
                "Selected tool",
                "Which tool ran (LLM mode — the configured one otherwise).",
            ),
            OutputVariable(
                "arguments",
                "Arguments",
                "The arguments it was actually invoked with.",
                type=OUTPUT_OBJECT,
            ),
            OutputVariable(
                "reason",
                "Reason",
                "Why the model chose that tool (LLM mode only).",
            ),
        ),
        searchable_terms=("tool", "mcp", "function", "llm", "choose"),
    ),
    NodeKind(
        kind="action.http_request",
        category=CATEGORY_ACTIONS,
        label="HTTP request",
        description="Call an external HTTP endpoint.",
        icon="Globe",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "method",
                "Method",
                FIELD_SELECT,
                required=True,
                default="GET",
                options=(
                    {"value": "GET", "label": "GET"},
                    {"value": "POST", "label": "POST"},
                    {"value": "PUT", "label": "PUT"},
                    {"value": "PATCH", "label": "PATCH"},
                    {"value": "DELETE", "label": "DELETE"},
                ),
            ),
            ConfigField("url", "URL", FIELD_VARIABLE_TEXT, required=True),
            ConfigField("headers", "Headers", FIELD_KEY_VALUE),
            ConfigField("body", "Body", FIELD_VARIABLE_TEXTAREA),
            ConfigField(
                "relay_group_id",
                "Route through",
                FIELD_SELECT,
                source=SOURCE_RELAY_GROUPS,
                # The leading "Platform default" choice is the platform's own
                # network: the request is sent directly.
                inheritable=True,
                help=(
                    "Send the request from a relay group's network, to reach an "
                    "on-premises endpoint. Platform default sends it directly."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("status", "Status", "The HTTP status code.", type=OUTPUT_NUMBER),
            OutputVariable("body", "Body", "The parsed response body."),
        ),
        searchable_terms=("api", "rest", "fetch", "curl"),
    ),
    NodeKind(
        kind="action.workset_add",
        category=CATEGORY_ACTIONS,
        label="Add to workset",
        description=(
            "Put one document into a wired workset, without an agent. Drop this "
            "inside a For-each over an integration's results and a whole "
            "reporting window is ingested for the cost of a loop — no agentic "
            "iterations spent on collection."
        ),
        icon="LibraryBig",
        # Adding queues the document for **embedding**: add_item returns
        # pending_documents, which _flush_workset_queue hands to
        # enqueue_document_processing. Classified False at first from a
        # docstring about referencing an already-embedded document.
        # (no requires_ai= here: the True default is correct)
        inputs=(_IN, _WORKSET_IN),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "title",
                "Title",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="{{loop.item.name}}",
            ),
            ConfigField(
                "content",
                "Content",
                FIELD_VARIABLE_TEXTAREA,
                placeholder="{{loop.item.description}}",
                help="The document's text. Leave empty to record metadata only.",
            ),
            ConfigField(
                "external_ref",
                "Source id",
                FIELD_VARIABLE_TEXT,
                placeholder="{{loop.item.id}}",
                help=(
                    "The source's own stable id. Map it whenever there is one: "
                    "it is what makes a re-run over an overlapping window skip "
                    "what it already has instead of duplicating it."
                ),
            ),
            ConfigField(
                "occurred_at",
                "Document date",
                FIELD_VARIABLE_TEXT,
                placeholder="{{loop.item.published}}",
                help=(
                    "The document's OWN date, not now. Date filters read this, "
                    "so mapping the wrong field re-dates the whole corpus."
                ),
            ),
            ConfigField("source_url", "Source URL", FIELD_VARIABLE_TEXT),
            ConfigField(
                "priority",
                "Priority",
                FIELD_VARIABLE_TEXT,
                help="Optional 0-100. Leave empty to let the summariser decide.",
            ),
            ConfigField(
                "pinned",
                "Never evict",
                FIELD_BOOLEAN,
                help="Protect this item when the corpus hits its budget.",
            ),
        ),
        output_variables=(
            OutputVariable("item_id", "Item ID", "The item that was written."),
            OutputVariable(
                "result",
                "Result",
                "created / unchanged / revised / updated — 'unchanged' is the "
                "overlap of a sliding window and costs nothing.",
            ),
            OutputVariable(
                "evicted",
                "Evicted",
                "How many items lost their content to make room.",
            ),
        ),
        searchable_terms=("workset", "corpus", "ingest", "collect", "add"),
    ),
    NodeKind(
        kind="action.workset_search",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_ACTIONS,
        label="Search workset",
        description=(
            "Find the passages in a wired workset that match a query. "
            "Deterministic retrieval — the hits come back as a list a For-each "
            "can iterate."
        ),
        icon="Search",
        inputs=(_IN, _WORKSET_IN),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "query",
                "Query",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="{{node.output}}",
            ),
            ConfigField(
                "limit", "Max hits", FIELD_NUMBER, default=10, min=1, max=50
            ),
            ConfigField(
                "min_priority",
                "Minimum priority",
                FIELD_NUMBER,
                min=0,
                max=100,
                help="Only search items at or above this score.",
            ),
        ),
        output_variables=(
            OutputVariable(
                "hits",
                "Hits",
                "Matching passages (item_id, title, snippet, chunk_index) — "
                "loop over it with For each.",
                type=OUTPUT_LIST,
            ),
            OutputVariable("count", "Count", "How many hits came back.", type=OUTPUT_NUMBER),
            OutputVariable(
                "coverage",
                "Coverage",
                "What the corpus holds behind this answer — zero hits with a "
                "full corpus means something different from zero hits with an "
                "empty one.",
            ),
        ),
        searchable_terms=("workset", "corpus", "search", "retrieve", "rag"),
    ),
    NodeKind(
        kind="action.workset_digest",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_ACTIONS,
        label="Workset digest",
        description=(
            "Roll a wired workset up for a synthesis: the highest-priority "
            "items with their summaries and citation handles, under a "
            "character budget, plus what was left out. Feed it to an agent's "
            "prompt instead of the corpus itself."
        ),
        icon="FileText",
        requires_ai=False,
        inputs=(_IN, _WORKSET_IN),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "top_n", "How many items", FIELD_NUMBER, default=20, min=1
            ),
            ConfigField(
                "max_chars",
                "Character budget",
                FIELD_NUMBER,
                default=12000,
                min=500,
            ),
            ConfigField(
                "require_ready",
                "Fail if still processing",
                FIELD_BOOLEAN,
                default=False,
                help=(
                    "Take the error path when items are still awaiting their "
                    "summaries, instead of digesting a half-processed corpus. "
                    "Use it when the synthesis claims to cover a whole window."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("digest", "Digest", "The rendered roll-up."),
            OutputVariable("included", "Included", "Items in the digest."),
            OutputVariable(
                "omitted",
                "Omitted",
                "Items left out for length — NOT for irrelevance.",
            ),
            OutputVariable("coverage", "Coverage", "The corpus's own summary."),
            OutputVariable(
                "complete",
                "Complete",
                "True only when the workset is sealed and nothing is pending "
                "or failed.",
                type=OUTPUT_BOOLEAN,
            ),
        ),
        searchable_terms=("workset", "corpus", "digest", "synthesis", "reduce"),
    ),
    NodeKind(
        kind="action.workset_seal",
        category=CATEGORY_ACTIONS,
        label="Seal workset",
        description=(
            "Declare that a wired workset has finished collecting. Until it is "
            "sealed no read will call it complete, because more could still "
            "arrive — so seal it after the ingestion loop and before the "
            "synthesis."
        ),
        icon="Lock",
        requires_ai=False,
        inputs=(_IN, _WORKSET_IN),
        outputs=(_OUT, _ERR),
        config_fields=(),
        output_variables=(
            OutputVariable("coverage", "Coverage", "What the sealed corpus holds."),
            OutputVariable(
                "complete",
                "Complete",
                "True when sealing left nothing pending or failed.",
                type=OUTPUT_BOOLEAN,
            ),
        ),
        searchable_terms=("workset", "corpus", "seal", "close", "finish"),
    ),
    # ── Integrations ────────────────────────────────────────────────────────
    NodeKind(
        kind="action.script",
        category=CATEGORY_ACTIONS,
        label="Script",
        description=(
            "Run a Custom tool's script (JavaScript or Python, sandboxed). "
            "The script IS a platform Custom tool — write it once here, "
            "reuse it from agents and other flows."
        ),
        icon="SquareCode",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "custom_tool_id",
                "Custom tool",
                FIELD_SELECT,
                required=True,
                source=SOURCE_CUSTOM_TOOLS,
                help=(
                    "The Custom tool whose script runs here. Create one "
                    "from this drawer — it lands in Tools like any other."
                ),
            ),
            ConfigField(
                "arguments",
                "Arguments",
                FIELD_JSON,
                help=(
                    "One value per declared input. Reference upstream "
                    "outputs with {{node.field}}; values are coerced "
                    "against the tool's signature."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("result", "Result", "What the script printed.", type=OUTPUT_TEXT),
            OutputVariable(
                "fields",
                "Fields",
                "When the result is a JSON object: {{node.fields.KEY}}.",
                type=OUTPUT_OBJECT,
            ),
        ),
        searchable_terms=(
            "script", "code", "custom tool", "python", "javascript",
            "sandbox", "function",
        ),
    ),
    NodeKind(
        kind="action.runner_command",
        category=CATEGORY_ACTIONS,
        label="Runner command",
        description=(
            "Run a command on an enrolled machine — shell, screenshot, "
            "file ops — and optionally wait for its result."
        ),
        icon="MonitorSmartphone",
        requires_ai=False,
        inputs=(_IN,),
        # Like Cast: the command failing is a pin, not a run failure —
        # denied / failed / timeout all route to `fail` with the error.
        outputs=(_branch("ok", "OK"), _branch("fail", "Fail")),
        config_fields=(
            ConfigField(
                "runner_id",
                "Runner",
                FIELD_SELECT,
                required=True,
                source=SOURCE_RUNNERS,
                help="The enrolled machine that executes the command.",
            ),
            ConfigField(
                "command_kind",
                "Command",
                FIELD_SELECT,
                required=True,
                default="run_shell",
                options=(
                    {"value": "run_shell", "label": "Run shell"},
                    {"value": "screenshot", "label": "Screenshot"},
                    {"value": "read_file", "label": "Read file"},
                    {"value": "write_file", "label": "Write file"},
                    {"value": "list_dir", "label": "List directory"},
                    {"value": "computer_action", "label": "Computer action"},
                    {"value": "browser_control", "label": "Browser control"},
                    {"value": "host_facts", "label": "Host facts"},
                ),
                help=(
                    "What the runner does. The runner's permission mode and "
                    "allow-lists still apply — a command outside them takes "
                    "the Fail pin with the refusal."
                ),
            ),
            ConfigField(
                "payload",
                "Payload",
                FIELD_JSON,
                placeholder='{"command": "echo hello"}',
                help=(
                    "Arguments for the command as JSON; {{node.field}} "
                    "references render first. Shapes: run_shell "
                    '{"command"}, read_file/list_dir {"path"}, write_file '
                    '{"path", "content"}, screenshot/host_facts {}.'
                ),
            ),
            ConfigField(
                "mode",
                "Mode",
                FIELD_SELECT,
                default="wait",
                options=(
                    {"value": "wait", "label": "Wait for the result"},
                    {"value": "dispatch", "label": "Fire and forget"},
                ),
                help=(
                    "Wait parks the run until the machine reports back "
                    "(approval-gated commands wait through the approval "
                    "too). Fire and forget continues immediately with the "
                    "command id — join later with Wait for event on "
                    "'Runner command completed'."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("command_id", "Command id", "The dispatched command."),
            OutputVariable(
                "status",
                "Status",
                "completed, failed, cancelled, timeout, denied — or "
                "pending / pending_approval when fired and forgotten.",
            ),
            OutputVariable(
                "result",
                "Result",
                "The structured result: {{node.result.output}} for shell, "
                "{{node.result.content}} for small text reads.",
            ),
            OutputVariable(
                "file_id",
                "File id",
                "Set when the result is file-backed (screenshots, large or "
                "binary reads) — the platform file holding the artifact.",
            ),
            OutputVariable("error", "Error", "The failure reason, if any."),
        ),
        searchable_terms=(
            "runner", "machine", "shell", "screenshot", "os", "command",
            "computer", "device",
        ),
    ),
    NodeKind(
        kind="action.update_work_item",
        category=CATEGORY_ACTIONS,
        label="Update work item",
        description=(
            "Add a progress note to a work item, change its status or "
            "complete it with a result."
        ),
        icon="ListChecks",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "work_item_id",
                "Work item id",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="{{create_item.work_item_id}}",
                help=(
                    "The item to change, usually an upstream node's output. "
                    "Only an item the flow's owner may write is changed."
                ),
            ),
            ConfigField(
                "operation",
                "Operation",
                FIELD_SELECT,
                required=True,
                default="note",
                options=(
                    {"value": "note", "label": "Add a progress note"},
                    {"value": "status", "label": "Change the status"},
                    {"value": "complete", "label": "Complete it"},
                ),
            ),
            ConfigField(
                "text",
                "Note or result",
                FIELD_VARIABLE_TEXTAREA,
                help=(
                    "The note to add, or the result a completed item keeps. "
                    "Ignored when changing the status."
                ),
            ),
            ConfigField(
                "status",
                "Status",
                FIELD_SELECT,
                default="in_progress",
                options=WORK_ITEM_STATUS_OPTIONS,
                help=(
                    "The new status, or for Complete it how it ended (done or "
                    "cancelled)."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("work_item_id", "Work item ID", "The changed item's id.", type=OUTPUT_TEXT),
            OutputVariable("status", "Status", "The item's status afterwards.", type=OUTPUT_TEXT),
        ),
        searchable_terms=("task", "todo", "note", "complete", "status", "progress"),
    ),
    NodeKind(
        kind="action.dispatch_to_fleet",
        category=CATEGORY_ACTIONS,
        label="Hand to fleet",
        description=(
            "Give a fleet a piece of work: a work item for its orchestrator, "
            "filed in the fleet's workspace, optionally waited for."
        ),
        icon="Boxes",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "fleet_id",
                "Fleet",
                FIELD_SELECT,
                required=True,
                source=SOURCE_FLEETS,
                help=(
                    "The item goes to the fleet's orchestrator in its "
                    "workspace, resolved when the node runs: a fleet with no "
                    "orchestrator takes the error path."
                ),
            ),
            ConfigField("title", "Title", FIELD_VARIABLE_TEXT, required=True),
            ConfigField("body", "Description", FIELD_VARIABLE_TEXTAREA),
            ConfigField(
                "priority",
                "Priority",
                FIELD_SELECT,
                default="normal",
                options=WORK_ITEM_PRIORITY_OPTIONS,
            ),
            ConfigField(
                "correlation_key",
                "Correlation key",
                FIELD_VARIABLE_TEXT,
                help=(
                    "An unfinished item with this key and title is reused "
                    "instead of handing the same work over twice."
                ),
            ),
            ConfigField(
                "wait",
                "Wait for completion",
                FIELD_BOOLEAN,
                help=(
                    "Park the run until the fleet completes the item; its "
                    "result is then published as {{node.result}}."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("work_item_id", "Work item ID", "The handed-over item's id.", type=OUTPUT_TEXT),
            OutputVariable("result", "Result", "The completed item's result (when waiting).", type=OUTPUT_TEXT),
        ),
        searchable_terms=("fleet", "team", "orchestrator", "delegate", "dispatch", "work item"),
    ),
    NodeKind(
        kind="action.workspace_add_source",
        category=CATEGORY_ACTIONS,
        label="Add workspace source",
        description=(
            "Add a text or a web page to a workspace's Sources, which every "
            "conversation filed there and every agent wired to it reads."
        ),
        icon="FolderPlus",
        # The document is chunked and embedded: an embedding provider runs.
        requires_ai=True,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "workspace_id",
                "Workspace",
                FIELD_SELECT,
                required=True,
                source=SOURCE_WORKSPACES,
                help="The flow's owner must be able to edit it.",
            ),
            ConfigField(
                "source_kind",
                "Source",
                FIELD_SELECT,
                default="text",
                options=(
                    {"value": "text", "label": "Text"},
                    {"value": "url", "label": "Web page"},
                ),
            ),
            ConfigField(
                "title",
                "Title",
                FIELD_VARIABLE_TEXT,
                help="The text source's name in the workspace (text only).",
            ),
            ConfigField(
                "content",
                "Text or URL",
                FIELD_VARIABLE_TEXTAREA,
                required=True,
                help=(
                    "The text to add, or the http(s) address of the page to "
                    "fetch (the platform's outbound safety rules apply)."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("document_id", "Document id", "The added source document.", type=OUTPUT_TEXT),
            OutputVariable(
                "knowledge_base_id",
                "Knowledge base id",
                "The workspace's Sources knowledge base.",
                type=OUTPUT_TEXT,
            ),
        ),
        searchable_terms=("workspace", "source", "knowledge", "document", "url", "add"),
    ),
    NodeKind(
        kind="action.create_report",
        category=CATEGORY_ACTIONS,
        label="Create report",
        description=(
            "Write a structured executive report from a spec - the document "
            "the Generate report tool produces - without an agent."
        ),
        icon="FileText",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField("title", "Title", FIELD_VARIABLE_TEXT, required=True),
            ConfigField(
                "header",
                "Header",
                FIELD_JSON,
                required=True,
                placeholder='{"title": "Weekly exposure", "verdict": "..."}',
                help=(
                    "The report's header object (it takes a 'title'). "
                    "{{node.field}} references render first."
                ),
            ),
            ConfigField(
                "blocks",
                "Blocks",
                FIELD_JSON,
                required=True,
                placeholder='[{"block": "paragraph", "text": "{{step.output}}"}]',
                help="The ordered blocks, each with its 'block' type.",
            ),
            ConfigField(
                "theme",
                "Theme",
                FIELD_SELECT,
                default="light",
                options=tuple({"value": t, "label": t.capitalize()} for t in REPORT_THEMES),
            ),
            ConfigField("footer", "Footer", FIELD_VARIABLE_TEXT),
            ConfigField(
                "template_id",
                "Template",
                FIELD_SELECT,
                source=SOURCE_REPORT_TEMPLATES,
                help="Dress the report in one of your report templates.",
            ),
            ConfigField(
                "report_id",
                "Report to revise",
                FIELD_VARIABLE_TEXT,
                help=(
                    "Empty creates a report; a report id of the flow's owner "
                    "rewrites that report in place (a new revision)."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("report_id", "Report id", "The written report.", type=OUTPUT_TEXT),
            OutputVariable("url", "URL", "Where the report opens on the platform.", type=OUTPUT_TEXT),
            OutputVariable("revision", "Revision", "The report's revision.", type=OUTPUT_NUMBER),
        ),
        searchable_terms=("report", "pdf", "document", "executive", "summary"),
    ),
    NodeKind(
        kind="action.export_report",
        category=CATEGORY_ACTIONS,
        label="Export report",
        description="Render one of the flow owner's reports to a PDF file.",
        icon="FileDown",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "report_id",
                "Report id",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="{{create_report.report_id}}",
            ),
        ),
        output_variables=_EXPORT_OUTPUTS,
        searchable_terms=("report", "pdf", "export", "download", "file"),
    ),
    NodeKind(
        kind="action.export_presentation",
        category=CATEGORY_ACTIONS,
        label="Export presentation",
        description=(
            "Render a presentation (a deck or an A4 / letter page) the flow's "
            "owner can open to a PDF or PowerPoint file."
        ),
        icon="Presentation",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "presentation_id",
                "Presentation id",
                FIELD_VARIABLE_TEXT,
                required=True,
                help="Usually the id an agent's Generate presentation step returned.",
            ),
            ConfigField(
                "format",
                "Format",
                FIELD_SELECT,
                default="pdf",
                options=(
                    {"value": "pdf", "label": "PDF"},
                    {"value": "pptx", "label": "PowerPoint (pictures)"},
                    {"value": "pptx_native", "label": "PowerPoint (editable)"},
                ),
            ),
        ),
        output_variables=_EXPORT_OUTPUTS,
        searchable_terms=("presentation", "deck", "slides", "pdf", "pptx", "export"),
    ),
    NodeKind(
        kind="action.render_video",
        category=CATEGORY_ACTIONS,
        label="Render video",
        description=(
            "Render a Video Studio video to MP4 and wait for it - the render "
            "the editor and the Video Studio assistant start."
        ),
        icon="Clapperboard",
        # The voice-over is synthesised by a speech model.
        requires_ai=True,
        inputs=(_IN,),
        # Like the runner command: the render failing is a pin, not a run
        # failure - a refused or failed render takes `fail` with its reason.
        outputs=(_branch("ok", "OK"), _branch("fail", "Fail")),
        config_fields=(
            ConfigField(
                "video_id",
                "Video id",
                FIELD_VARIABLE_TEXT,
                required=True,
                help="A video the flow's owner may edit.",
            ),
            ConfigField(
                "quality",
                "Quality",
                FIELD_SELECT,
                default="high",
                options=(
                    {"value": "draft", "label": "Draft (720p)"},
                    {"value": "standard", "label": "Standard (1080p30)"},
                    {"value": "high", "label": "High (1080p60)"},
                    {"value": "ultra", "label": "Ultra (2160p60)"},
                ),
            ),
        ),
        output_variables=(
            OutputVariable("render_id", "Render id", "The render.", type=OUTPUT_TEXT),
            OutputVariable(
                "status", "Status", "ready, failed, cancelled - or refused.", type=OUTPUT_TEXT
            ),
            OutputVariable("file_id", "File id", "The MP4, once rendered.", type=OUTPUT_TEXT),
            OutputVariable("url", "URL", "Where the video opens on the platform.", type=OUTPUT_TEXT),
            OutputVariable("error", "Error", "Why it was refused or failed.", type=OUTPUT_TEXT),
        ),
        searchable_terms=("video", "render", "mp4", "studio", "demo", "movie"),
    ),
    NodeKind(
        kind="action.deep_investigation",
        category=CATEGORY_ACTIONS,
        label="Deep investigation",
        description=(
            "Investigate a subject across its source packs and get one report "
            "whose every statement cites its source."
        ),
        icon="Telescope",
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "subject",
                "Subject",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="APT29, CVE-2024-3400, evil.example.com",
            ),
            ConfigField(
                "source_packs",
                "Source packs",
                FIELD_JSON,
                placeholder='["web", "opencti"]',
                help=(
                    "The pack slugs to query, as a JSON list. Empty: the "
                    "packs recommended for the subject's type."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("investigation_id", "Investigation id", "The run.", type=OUTPUT_TEXT),
            OutputVariable("report", "Report", "The cited report, in Markdown.", type=OUTPUT_TEXT),
        ),
        searchable_terms=("investigation", "research", "cti", "osint", "report", "citations"),
    ),
    NodeKind(
        kind="integration",
        side_effect=SIDE_EFFECT_TOOL,
        category=CATEGORY_INTEGRATIONS,
        label="Integration",
        description="Run an operation on a connected integration.",
        icon="Plug",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "integration_id",
                "Integration",
                FIELD_SELECT,
                required=True,
                source=SOURCE_INTEGRATIONS,
            ),
            ConfigField("operation", "Operation", FIELD_TEXT, required=True),
            ConfigField("parameters", "Parameters", FIELD_JSON),
            ConfigField(
                "fail_on_error_result",
                "Fail when the operation answers with an error",
                FIELD_BOOLEAN,
                default=False,
                help=(
                    "Take the error path when the operation answers with an "
                    "error, for example a lookup that finds nothing. Off: the "
                    "step completes and passes the answer on as its result."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("result", "Result", "The operation's return value."),
            OutputVariable(
                "fields",
                "Fields",
                "When the operation answers with an object (a created entity, a "
                "GraphQL query, ...): {{node.fields.KEY}}.",
                type=OUTPUT_OBJECT,
            ),
            OutputVariable(
                "items",
                "Items",
                "When the operation answers with a list (a listing): loop over it "
                "with Loop, or list one key with {{node.items.KEY}}.",
                type=OUTPUT_LIST,
            ),
        ),
        searchable_terms=(
            "github", "jira", "connector", "opencti", "query", "list", "search",
            "create",
        ),
    ),
    # ── Flows ───────────────────────────────────────────────────────────────
    NodeKind(
        kind="flow.call",
        side_effect=SIDE_EFFECT_NESTED,
        category=CATEGORY_AGENTS,
        label="Flow",
        description="Hand off to another flow and continue when it returns.",
        icon="Workflow",
        requires_ai=False,
        # Accepts the handover channel too: an agent's handover port may target
        # a whole flow, and the call is a SYNCHRONOUS cycle — the caller's out
        # branch resumes only once this flow returned (the walker parks the
        # run, so nothing downstream of the agent runs while the child does).
        inputs=(
            Port(
                id="in",
                label="In",
                types=(PORT_FLOW, PORT_BRANCH, PORT_ERROR, PORT_HANDOVER),
                multiple=True,
            ),
            # A budget wired HERE bounds every agent of the flow being called,
            # and propagates down its own Flow nodes — one ceiling for
            # a whole composed pipeline, without editing any of its agents.
            # Same node type the agent sockets take, so the SAME Budget node
            # can serve both.
            Port(
                id="budget",
                label="Budget",
                types=(PORT_CAP_BUDGET,),
                multiple=True,
            ),
        ),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "flow_id",
                "Flow",
                FIELD_SELECT,
                required=True,
                source=SOURCE_FLOWS,
                help="The flow this step hands off to.",
            ),
            ConfigField(
                "input",
                "Input",
                FIELD_VARIABLE_TEXTAREA,
                help="What to pass to that flow. Reference upstream outputs with {{node.field}}.",
            ),
        ),
        output_variables=(
            OutputVariable("output", "Output", "What the called flow returned."),
                    OutputVariable(
                "child_run_id",
                "Child run id",
                "The relay run's id (always published).",
            ),
            OutputVariable(
                "detached",
                "Detached",
                "True when the call did not wait for the child.",
                type=OUTPUT_BOOLEAN,
            ),
),
        searchable_terms=("subflow", "sub-flow", "call", "compose", "nested"),
    ),
    # ── Utilities ───────────────────────────────────────────────────────────
    NodeKind(
        kind="util.expression",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Expression",
        description=(
            "Evaluate a sandboxed expression — math, text, JSON and list "
            "helpers — without an LLM. References render first."
        ),
        icon="Sigma",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "expression",
                "Expression",
                FIELD_VARIABLE_TEXTAREA,
                required=True,
                placeholder='round({{score.value}} * 100) if the math kind, or join(", ", split({{ag.output}}, "\n"))',
                help=(
                    "Helpers: abs round min max sum int float ceil floor · "
                    "str len upper lower strip title replace split join "
                    "startswith endswith contains slice · json get at first "
                    "last sort unique append count keys values bool."
                ),
            ),
        ),
        output_variables=(
            OutputVariable("value", "Value", "The expression's result."),
            OutputVariable(
                "fields",
                "Fields",
                "When the result is an object: {{node.fields.KEY}}.",
                type=OUTPUT_OBJECT,
            ),
        ),
        searchable_terms=(
            "expression", "math", "formula", "compute", "string",
            "concat", "array", "pure",
        ),
    ),
    NodeKind(
        kind="util.reroute",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Reroute",
        description=(
            "A knot: passes execution and its input straight through — "
            "purely for untangling wires on busy graphs."
        ),
        icon="Waypoints",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT,),
        output_variables=(
            OutputVariable("value", "Value", "The upstream node's primary output."),
        ),
        searchable_terms=("reroute", "knot", "elbow", "wire", "tidy"),
    ),
    NodeKind(
        kind="util.code",
        category=CATEGORY_UTILITIES,
        label="Run code",
        description=(
            "Run Python in the platform sandbox (OpenSandbox, or the local "
            "isolated fallback) — the same mechanism as the code interpreter."
        ),
        icon="Terminal",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "code",
                "Python code",
                FIELD_VARIABLE_TEXTAREA,
                required=True,
                placeholder="print(inputs[\"texte\"].upper())",
                help=(
                    "Whatever the script prints becomes {{node.output}}. "
                    "Prefer the Inputs map below over inline {{refs}} — it "
                    "arrives as the `inputs` dict, no quoting pitfalls."
                ),
            ),
            ConfigField(
                "inputs",
                "Inputs",
                FIELD_KEY_VALUE,
                help="Rendered values exposed to the script as `inputs[name]`.",
            ),
            ConfigField(
                "timeout",
                "Timeout (seconds)",
                FIELD_NUMBER,
                default=60,
                help="Hard cap: 300.",
            ),
        ),
        output_variables=(
            OutputVariable("output", "Output", "The script's stdout.", type=OUTPUT_TEXT),
            OutputVariable(
                "fields",
                "Fields",
                "When stdout is a JSON object: {{node.fields.KEY}}.",
                type=OUTPUT_OBJECT,
            ),
        ),
        searchable_terms=("python", "code", "script", "sandbox", "interpreter"),
    ),
    NodeKind(
        kind="util.set_variable",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Set variable",
        description=(
            "Write a run-scoped variable — mutable state the whole run "
            "shares. Read it back with Get variable (or a Gate)."
        ),
        icon="SquarePen",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "name",
                "Variable",
                FIELD_TEXT,
                required=True,
                placeholder="compteur",
                help="Identifier-shaped; shared by every node of this run.",
            ),
            ConfigField(
                "mode",
                "Mode",
                FIELD_SELECT,
                default="set",
                options=(
                    {"value": "set", "label": "Set"},
                    {"value": "append", "label": "Append (text)"},
                    {"value": "increment", "label": "Increment (number)"},
                ),
            ),
            ConfigField(
                "value",
                "Value",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="{{node.output}}",
                help="Increment coerces to a number (empty starts at 0).",
            ),
        ),
        output_variables=(
            OutputVariable("value", "Value", "The variable's new value."),
        ),
        searchable_terms=("variable", "state", "set", "counter", "assign"),
    ),
    NodeKind(
        kind="util.get_variable",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Get variable",
        description=(
            "Read a run-scoped variable written by Set variable — or a "
            "platform collection (runners, agents, flows…) as a list a "
            "Loop can iterate."
        ),
        icon="Variable",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "source",
                "Source",
                FIELD_SELECT,
                default="flow",
                options=(
                    {"value": "flow", "label": "Flow variable"},
                    {"value": "collection", "label": "Platform collection"},
                ),
                help=(
                    "A flow variable reads what Set variable wrote in this "
                    "run; a platform collection lists entities visible to "
                    "the person who runs the flow."
                ),
            ),
            # Contextually required (validated per source) — a catalog-level
            # `required` would false-flag the mode that does not use it.
            ConfigField(
                "name", "Variable", FIELD_TEXT,
                placeholder="compteur",
            ),
            ConfigField(
                "default",
                "Default",
                FIELD_VARIABLE_TEXT,
                help="Published when the variable was never set.",
            ),
            ConfigField(
                "collection",
                "Collection",
                FIELD_SELECT,
                default="runners",
                options=(
                    {"value": "runners", "label": "Runners"},
                    {"value": "agents", "label": "Agents"},
                    {"value": "flows", "label": "Flows"},
                    {"value": "custom_tools", "label": "Custom tools"},
                    {"value": "knowledge_bases", "label": "Knowledge bases"},
                    {"value": "event_types", "label": "Event types"},
                ),
                help=(
                    "Each item is an object — read fields downstream as "
                    "{{loop.item.name}}, {{loop.item.id}}… Feed {{node.value}} "
                    "to a Loop to act on every item."
                ),
            ),
        ),
        output_variables=(
            OutputVariable(
                "value",
                "Value",
                "The variable's current value — a list when reading a "
                "collection (iterable with the Loop node).",
            ),
            OutputVariable(
                "count",
                "Count",
                "Number of items when the value is a list, 0 otherwise.",
                type=OUTPUT_NUMBER,
            ),
        ),
        searchable_terms=(
            "variable", "state", "get", "read", "list", "collection",
            "runners", "foreach",
        ),
    ),
    NodeKind(
        kind="util.while",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="While",
        description=(
            "Repeat the body while the condition holds — re-evaluated before "
            "every pass (pair it with Set variable to converge)."
        ),
        icon="IterationCw",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(
            Port(id="body", label="Body", types=(PORT_FLOW,), multiple=True),
            Port(id="done", label="Done", types=(PORT_FLOW,), multiple=True),
        ),
        config_fields=(
            ConfigField(
                "left", "Value", FIELD_VARIABLE_TEXT, required=True,
                placeholder="{{compteur.value}}",
            ),
            ConfigField(
                "operator",
                "Operator",
                FIELD_SELECT,
                required=True,
                default="lt",
                options=(
                    {"value": "equals", "label": "equals"},
                    {"value": "not_equals", "label": "does not equal"},
                    {"value": "contains", "label": "contains"},
                    {"value": "not_contains", "label": "does not contain"},
                    {"value": "matches", "label": "matches regex"},
                    {"value": "gt", "label": "is greater than"},
                    {"value": "lt", "label": "is less than"},
                    {"value": "is_empty", "label": "is empty"},
                    {"value": "is_not_empty", "label": "is not empty"},
                ),
            ),
            ConfigField("right", "Compared to", FIELD_VARIABLE_TEXT),
            ConfigField(
                "max_iterations",
                "Max passes",
                FIELD_NUMBER,
                default=25,
                help="Hard cap — the loop exits through Done past it.",
            ),
        ),
        output_variables=(
            OutputVariable("passes", "Passes", "How many passes ran so far.", type=OUTPUT_NUMBER),
        ),
        searchable_terms=("while", "until", "loop", "repeat", "converge"),
    ),
    NodeKind(
        kind="util.do_once",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Do once",
        description=(
            "The first execution leaves through First; every later one "
            "through Again."
        ),
        icon="CircleDot",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(
            Port(id="first", label="First", types=(PORT_FLOW,), multiple=True),
            Port(id="again", label="Again", types=(PORT_FLOW,), multiple=True),
        ),
        output_variables=(
            OutputVariable("count", "Count", "How many times it was reached.", type=OUTPUT_NUMBER),
        ),
        searchable_terms=("once", "first", "dedupe", "initialisation"),
    ),
    NodeKind(
        kind="util.flip_flop",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Flip flop",
        description="Alternates between A and B on each execution.",
        icon="ArrowLeftRight",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(
            Port(id="a", label="A", types=(PORT_FLOW,), multiple=True),
            Port(id="b", label="B", types=(PORT_FLOW,), multiple=True),
        ),
        output_variables=(
            OutputVariable("is_a", "Is A", "True on A passes, false on B.", type=OUTPUT_BOOLEAN),
        ),
        searchable_terms=("flipflop", "alternate", "toggle", "ab"),
    ),
    NodeKind(
        kind="util.gate",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Gate",
        description=(
            "Passes only while its variable is truthy — open and close it "
            "with Set variable (true/false)."
        ),
        icon="DoorOpen",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(
            _OUT,
            Port(id="blocked", label="Blocked", types=(PORT_FLOW,), multiple=True),
        ),
        config_fields=(
            ConfigField(
                "variable",
                "Variable",
                FIELD_TEXT,
                required=True,
                placeholder="porte_ouverte",
                help="The run variable that opens (truthy) or closes the gate.",
            ),
            ConfigField(
                "default_open",
                "Open by default",
                FIELD_BOOLEAN,
                help="Applies while the variable was never set.",
            ),
        ),
        output_variables=(
            OutputVariable("open", "Open", "Whether the gate let this pass through.", type=OUTPUT_BOOLEAN),
        ),
        searchable_terms=("gate", "open", "close", "block", "guard"),
    ),
    NodeKind(
        kind="util.primitive",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Value",
        description=(
            "A constant: a typed literal, or a platform variable. Never "
            "executed — reference it anywhere as {{node.value}}."
        ),
        icon="Equal",
        requires_ai=False,
        # No exec ports: a constant sits outside control flow (UE literal
        # pin). The single `value` pin is a data affordance — dragging it out
        # creates a node prefilled with the reference.
        outputs=(
            Port(id="value", label="Value", types=(PORT_PARAM,), multiple=True),
        ),
        config_fields=(
            ConfigField(
                "value_type",
                "Type",
                FIELD_SELECT,
                required=True,
                default="text",
                options=(
                    {"value": "text", "label": "Text"},
                    {"value": "number", "label": "Number"},
                    {"value": "boolean", "label": "Boolean"},
                    {"value": "json", "label": "JSON"},
                    {"value": "object", "label": "Object (declared type)"},
                ),
            ),
            ConfigField(
                "object_type_id",
                "Object type",
                FIELD_SELECT,
                source=SOURCE_EVENT_TYPES,
                help=(
                    "Object constants: the declared type this value conforms "
                    "to — its fields become the form below, coerced against "
                    "the signature. Read downstream as {{node.value.FIELD}}."
                ),
            ),
        ),
        output_variables=(
            OutputVariable(
                "value",
                "Value",
                "The constant — the literal, the platform variable's value, "
                "or the typed object.",
            ),
        ),
        searchable_terms=(
            "constant", "literal", "primitive", "variable", "text",
            "number", "boolean", "value",
        ),
    ),
    NodeKind(
        kind="util.wait_event",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Wait for event",
        description=(
            "Pause the run until a declared event is emitted, then continue "
            "with its fields."
        ),
        icon="Hourglass",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "event_type_id",
                "Event type",
                FIELD_SELECT,
                required=True,
                source=SOURCE_EVENT_TYPES,
                help=(
                    "Resumes on the first matching emission AFTER this node "
                    "parks — scoped by the emitter (run / flow / global)."
                ),
            ),
        ),
        output_variables=(
            OutputVariable(
                "fields",
                "Fields",
                "The event's typed fields — read as {{node.fields.NAME}}.",
                type=OUTPUT_OBJECT,
            ),
            OutputVariable(
                "event",
                "Event",
                "Emission metadata: {{node.event.name}}, {{node.event.id}}.",
                type=OUTPUT_OBJECT,
            ),
        ),
        searchable_terms=("event", "wait", "listen", "until", "bind"),
    ),
    NodeKind(
        kind="util.delay",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Delay",
        description="Pause before continuing.",
        icon="Timer",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "duration",
                "Duration",
                FIELD_TEXT,
                required=True,
                placeholder="15m",
                help="Accepts the platform delay syntax: 30s, 15m, 2h, 1d.",
            ),
        ),
        output_variables=(
            OutputVariable("input", "Input", "The value passed straight through."),
        ),
        searchable_terms=("wait", "sleep", "pause"),
    ),
    NodeKind(
        kind="util.loop",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Loop",
        description="Repeat the downstream branch for each item.",
        icon="Repeat",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(
            Port(id="each", label="Each item", types=(PORT_FLOW,), multiple=True),
            Port(id="done", label="Done", types=(PORT_FLOW,), multiple=True),
        ),
        config_fields=(
            ConfigField(
                "items",
                "Items",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="{{node.output}}",
                help="A list to iterate over.",
            ),
            ConfigField(
                "max_iterations",
                "Max iterations",
                FIELD_NUMBER,
                default=100,
                help="Hard cap — the loop stops here even if more items remain.",
            ),
        ),
        output_variables=(
            OutputVariable("item", "Item", "The current item."),
            OutputVariable("index", "Index", "The current item's zero-based index.", type=OUTPUT_NUMBER),
        ),
        searchable_terms=("for each", "iterate", "repeat"),
    ),
    NodeKind(
        kind="util.merge",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Merge",
        description="Join several branches back into one.",
        icon="GitMerge",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "strategy",
                "Strategy",
                FIELD_SELECT,
                required=True,
                default="wait_all",
                options=(
                    {"value": "wait_all", "label": "Wait for every branch"},
                    {"value": "first_wins", "label": "Continue with the first branch"},
                    {"value": "concat", "label": "Concatenate every result"},
                ),
            ),
        ),
        output_variables=(
            OutputVariable("merged", "Merged", "The combined upstream results."),
        ),
        searchable_terms=("join", "combine", "collect"),
    ),
    # ── Blueprint-style data utilities ───────────────────────────────────────
    # Lifted from the vocabulary Unreal Engine's Blueprints made familiar:
    # Break a struct apart, Cast with an explicit failure pin, Select a value,
    # Sequence ordered branches, and a Return-style Stop.
    NodeKind(
        kind="util.break",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Break",
        description="Break a JSON object apart so each field is referenceable.",
        icon="Ungroup",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT, _ERR),
        config_fields=(
            ConfigField(
                "value",
                "Value",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="{{agent.output}}",
                help=(
                    "A JSON object (an agent's structured output, a webhook "
                    "payload…). Downstream, read fields as {{node.fields.NAME}}."
                ),
            ),
        ),
        output_variables=(
            OutputVariable(
                "fields", "Fields", "The object's fields — {{node.fields.NAME}}.",
                type=OUTPUT_OBJECT,
            ),
            OutputVariable("text", "Text", "The raw value, untouched."),
        ),
        searchable_terms=("struct", "parse", "json", "decompose"),
    ),
    NodeKind(
        kind="util.cast",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Cast",
        description="Convert a value to a type — with a branch for when it can't.",
        icon="Replace",
        requires_ai=False,
        inputs=(_IN,),
        # Cast never fails the run: like a Blueprint cast, failure is a pin.
        outputs=(_branch("ok", "OK"), _branch("fail", "Fail")),
        config_fields=(
            ConfigField(
                "value",
                "Value",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="{{node.output}}",
            ),
            ConfigField(
                "to",
                "Cast to",
                FIELD_SELECT,
                required=True,
                default="number",
                options=(
                    {"value": "number", "label": "Number"},
                    {"value": "boolean", "label": "Boolean"},
                    {"value": "json", "label": "JSON"},
                    {"value": "text", "label": "Text"},
                ),
            ),
        ),
        output_variables=(
            OutputVariable("value", "Value", "The converted value (original on Fail)."),
        ),
        searchable_terms=("convert", "type", "coerce", "number", "boolean"),
    ),
    NodeKind(
        kind="util.select",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Select",
        description="Pick a value from a mapping by key, with a default.",
        icon="ListFilter",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "key",
                "Key",
                FIELD_VARIABLE_TEXT,
                required=True,
                placeholder="{{cond.matched}}",
            ),
            ConfigField(
                "options",
                "Options",
                FIELD_KEY_VALUE,
                required=True,
                help="One entry per key: the value chosen when the key matches.",
            ),
            ConfigField(
                "default",
                "Default",
                FIELD_VARIABLE_TEXT,
                help="Used when no key matches.",
            ),
        ),
        output_variables=(
            OutputVariable("value", "Value", "The chosen value."),
        ),
        searchable_terms=("pick", "map", "lookup", "choose"),
    ),
    NodeKind(
        kind="util.sequence",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Sequence",
        description="Start several branches in a fixed order.",
        icon="ListOrdered",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(
            Port(id="then_1", label="Then 1", types=(PORT_FLOW,), multiple=True),
            Port(id="then_2", label="Then 2", types=(PORT_FLOW,), multiple=True),
            Port(id="then_3", label="Then 3", types=(PORT_FLOW,), multiple=True),
        ),
        searchable_terms=("order", "then", "steps", "chain"),
    ),
    NodeKind(
        kind="util.stop",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Stop",
        description="End the run here — nothing queued after it executes.",
        icon="CircleStop",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(),
        config_fields=(
            ConfigField(
                "result",
                "Result",
                FIELD_VARIABLE_TEXTAREA,
                help="Optional closing message — it becomes the run's summary.",
            ),
        ),
        output_variables=(
            OutputVariable("result", "Result", "The closing message."),
        ),
        searchable_terms=("break", "return", "end", "halt", "exit"),
    ),
    NodeKind(
        kind="util.group",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Group",
        description=(
            "A visual container: group a selection to move and read it as "
            "one block. Never executed."
        ),
        icon="Group",
        requires_ai=False,
        is_annotation=True,
        searchable_terms=("group", "container", "frame", "region", "box"),
    ),
    NodeKind(
        kind="util.note",
        side_effect=SIDE_EFFECT_NONE,
        category=CATEGORY_UTILITIES,
        label="Note",
        description="A free-floating annotation. Never executed.",
        icon="StickyNote",
        requires_ai=False,
        config_fields=(ConfigField("text", "Text", FIELD_TEXTAREA),),
        is_annotation=True,
        searchable_terms=("comment", "sticky", "annotation"),
    ),
    # ── Import fallback ─────────────────────────────────────────────────────
    NodeKind(
        kind=KIND_UNRESOLVED,
        category=CATEGORY_OTHER,
        label="Unresolved node",
        description=(
            "An imported node with no equivalent on this platform. Its original "
            "definition is preserved so it can be re-mapped by hand."
        ),
        icon="HelpCircle",
        requires_ai=False,
        inputs=(_IN,),
        outputs=(_OUT,),
        config_fields=(
            ConfigField(
                "source_type",
                "Original type",
                FIELD_TEXT,
                help="The component type this node had in the file it came from.",
            ),
        ),
        searchable_terms=("import", "unknown", "placeholder"),
    ),
)

# ── Utility node shapes ─────────────────────────────────────────────
#
# Flowchart glyphs for the utilities: presentation only (the walker never
# reads `shape`), so the builder strips it from the semantic comparison the
# same way it strips node positions. Defaults follow flowchart conventions —
# a terminator is a circle, a loop is a hexagon (preparation), a merge is a
# diamond, a wait/delay is a pill.

SHAPE_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "rectangle", "label": "Rectangle"},
    {"value": "diamond", "label": "Diamond"},
    {"value": "circle", "label": "Circle"},
    {"value": "hexagon", "label": "Hexagon"},
    {"value": "parallelogram", "label": "Parallelogram"},
    {"value": "pill", "label": "Pill"},
)

DEFAULT_SHAPES: dict[str, str] = {
    "util.primitive": "pill",
    "util.stop": "circle",
    "util.loop": "hexagon",
    "util.while": "hexagon",
    "util.gate": "diamond",
    "util.expression": "parallelogram",
    "util.merge": "diamond",
    "util.delay": "pill",
    "util.wait_event": "pill",
}


_VALUES_IN = Port(
    id="values",
    label="Values",
    types=(PORT_PARAM,),
    multiple=True,
)

_VALUES_PORT_CATEGORIES = frozenset(
    {
        CATEGORY_CONDITIONS,
        CATEGORY_ACTIONS,
        CATEGORY_UTILITIES,
        CATEGORY_INTEGRATIONS,
    }
)

# Kind-level opt-in for categories that must NOT take the port wholesale:
# flow.call consumes data wires (typed arguments) but now lives in the
# merged "Agents & Flows" section, whose agent siblings do not.
_VALUES_PORT_KINDS = frozenset({"flow.call"})


def _with_values_port(nk: NodeKind) -> NodeKind:
    """Give every executable consumer a `values` data socket.

    Value nodes and declared parameters plug in there — multiple of them —
    and the connection auto-inserts the `{{...}}` reference into the node's
    first empty field. Not on triggers (they produce), capabilities (they
    attach to agents) or the Value node itself.
    """
    if (
        nk.category not in _VALUES_PORT_CATEGORIES
        and nk.kind not in _VALUES_PORT_KINDS
    ) or nk.is_annotation:
        return nk
    if nk.kind == "util.primitive" or not nk.inputs:
        return nk
    import dataclasses

    return dataclasses.replace(nk, inputs=(*nk.inputs, _VALUES_IN))


def _with_shape_field(nk: NodeKind) -> NodeKind:
    """Give every executable utility a `shape` select — one place, not nine."""
    if nk.category != CATEGORY_UTILITIES or nk.is_annotation:
        return nk
    import dataclasses

    return dataclasses.replace(
        nk,
        config_fields=(
            *nk.config_fields,
            ConfigField(
                "shape",
                "Shape",
                FIELD_SELECT,
                options=SHAPE_OPTIONS,
                default=DEFAULT_SHAPES.get(nk.kind, "rectangle"),
                help="Presentation only — the flowchart glyph this node draws as.",
            ),
        ),
    )


NODE_KINDS = tuple(_with_values_port(_with_shape_field(nk)) for nk in NODE_KINDS)

NODE_KINDS_BY_ID: dict[str, NodeKind] = {nk.kind: nk for nk in NODE_KINDS}


# ─── AI dependency of a graph (FR-014) ────────────────────────────────

# The node kind that hands off to another flow. Its callee is resolvable
# (``config.flow_id``), so the AI question can be answered across a call rather
# than guessed at: see :func:`graph_ai_dependency`.
FLOW_CALL_KIND = "flow.call"

# Kinds that grow one output socket per entry of their `cases` config
# (`case:<name>`, resolved by `get_port`): every kind with a `case` output - a
# Switch's cases, a Classify node's labels. Derived from the port, as the
# builder does, so a new kind cannot get the sockets without the validation.
PER_CASE_SOCKET_KINDS: frozenset[str] = frozenset(
    nk.kind for nk in NODE_KINDS if any(p.id == "case" for p in nk.outputs)
)


def graph_ai_dependency(graph: dict) -> tuple[bool, set[str]]:
    """``(this graph needs AI, the flow ids it hands off to)``.

    Annotations are skipped — they are not part of the executable graph, so a
    sticky note about an agent must not hold a deterministic flow back.

    An **unknown** kind counts as requiring AI. A graph naming a node type this
    build does not have is either newer than this build or corrupt, and neither
    is a reason to assume no model is involved.

    Says nothing about called flows itself: resolving those needs the database,
    and this stays pure so the classification is testable without one.
    """
    needs_ai = False
    called: set[str] = set()
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        kind = node.get("kind") or ""
        spec = NODE_KINDS_BY_ID.get(kind)
        if spec is not None and spec.is_annotation:
            continue
        if kind == FLOW_CALL_KIND:
            target = (node.get("config") or {}).get("flow_id")
            if isinstance(target, str) and target:
                called.add(target)
            # A call whose target is unset executes nothing, so it adds no AI
            # dependency of its own.
            continue
        if spec is None or spec.requires_ai:
            needs_ai = True
    return needs_ai, called


# ─── Lookups used by the validator and the API ───────────────────────────────


def get_node_kind(kind: str) -> NodeKind | None:
    """Return the definition for *kind*, or ``None`` when it is unknown."""
    return NODE_KINDS_BY_ID.get(kind)


def get_port(
    node_kind: NodeKind, port_id: str | None, *, output: bool
) -> Port | None:
    """Return the named input or output port of *node_kind*.

    Ports are declared statically per kind, with one dynamic family: a Switch
    (and a Classify node, :data:`PER_CASE_SOCKET_KINDS`) grows one output
    socket per configured case (``case:<name>``, the Unreal Blueprint shape).
    Those resolve to the declared ``case`` port — same channel types, so every
    compatibility rule applies unchanged.
    """
    # An edge may arrive without a handle at all (hand-written import, older
    # export). The walker reads a missing source handle as `out` — resolve
    # the same default here so validation and execution agree; a kind without
    # that port falls through to "no such port", never a crash.
    if not port_id:
        port_id = "out" if output else "in"
    ports = node_kind.outputs if output else node_kind.inputs
    for port in ports:
        if port.id == port_id:
            return port
    if (
        output
        and port_id.startswith("param:")
        and node_kind.kind == "trigger.manual"
    ):
        # Declared-parameter pins are minted from config; they all share the
        # PORT_PARAM channel, so compatibility rules apply unchanged.
        return Port(
            id=port_id,
            label=port_id.removeprefix("param:"),
            types=(PORT_PARAM,),
            multiple=True,
        )
    if output and port_id.startswith("case:"):
        for port in ports:
            if port.id == "case":
                return Port(
                    id=port_id,
                    label=port_id.removeprefix("case:"),
                    types=port.types,
                    multiple=port.multiple,
                )
    return None


def allowed_edge_kinds(source: Port, target: Port) -> set[str]:
    """Edge kinds a connection between these two ports may carry.

    Empty means the ports are incompatible and the connection must be refused.
    """
    shared = set(source.types) & set(target.types)
    return {EDGE_KIND_BY_PORT_TYPE[t] for t in shared if t in EDGE_KIND_BY_PORT_TYPE}


def output_variable_names(kind: str) -> set[str]:
    """Names a node of *kind* publishes for ``{{node_id.<name>}}`` references."""
    node_kind = get_node_kind(kind)
    if node_kind is None:
        return set()
    return {var.name for var in node_kind.output_variables}


def output_variable(kind: str, name: str) -> OutputVariable | None:
    """The output *name* a node of *kind* publishes, if it declares one."""
    node_kind = get_node_kind(kind)
    if node_kind is None:
        return None
    return next((var for var in node_kind.output_variables if var.name == name), None)


def variable_field_names(kind: str) -> set[str]:
    """Config fields of *kind* that accept ``{{...}}`` references."""
    node_kind = get_node_kind(kind)
    if node_kind is None:
        return set()
    return {
        f.name
        for f in node_kind.config_fields
        if f.type in (FIELD_VARIABLE_TEXT, FIELD_VARIABLE_TEXTAREA)
    }


def has_llm_socket(kind: str) -> bool:
    """True when *kind* accepts an LLM on an input port (condition nodes)."""
    node_kind = get_node_kind(kind)
    if node_kind is None:
        return False
    return any(PORT_CAP_LLM in port.types for port in node_kind.inputs)


def field_applies(field: ConfigField, llm_wired: bool) -> bool:
    """Whether *field* belongs to the mode a node is currently operating in.

    A field with no ``mode`` always applies. The UI renders, and the validator
    requires, exactly the fields this returns True for — so a deterministic
    node is never asked for an LLM question, and vice versa.
    """
    if field.mode is None:
        return True
    return field.mode == (MODE_LLM if llm_wired else MODE_DETERMINISTIC)


def serialize_node_kind(node_kind: NodeKind) -> dict[str, Any]:
    """JSON-ready form of a node kind, as served by the catalog endpoint."""
    return asdict(node_kind)


def static_catalog() -> list[dict[str, Any]]:
    """The full static catalog, JSON-ready, in library-panel display order."""
    order = {category: i for i, category in enumerate(CATEGORY_ORDER)}
    ordered = sorted(
        NODE_KINDS, key=lambda nk: (order.get(nk.category, len(order)), nk.label)
    )
    return [serialize_node_kind(nk) for nk in ordered]


def categories() -> list[dict[str, str]]:
    """The library panel's sections, in display order."""
    return [
        {"id": category, "label": CATEGORY_LABELS[category]}
        for category in CATEGORY_ORDER
    ]
