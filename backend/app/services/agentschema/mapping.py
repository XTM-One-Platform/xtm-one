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

"""Mapping between the XTM One agent model and Microsoft AgentSchema.

Two directions, deliberately asymmetric:

- :func:`build_agentschema_document` turns a *native export bundle* (the
  ``version: 3`` dict the existing ``GET /agents/{id}/export`` already builds)
  into a spec-valid ``PromptAgent`` document. Working from the bundle rather
  than from the ORM object means there is exactly one place that decides what
  leaves the platform, so a field that is secret-stripped or omitted for the
  native format cannot leak through the AgentSchema format.
- :func:`agentschema_to_native_bundle` turns a validated AgentSchema document
  back into that same native bundle shape, so the import endpoint feeds the
  *one* agent-creation pipeline (:mod:`app.services.agent_import`) instead of
  growing a parallel one.

Fields AgentSchema cannot express are reported (never silently dropped) by
:func:`portability_report` and preserved verbatim under
``metadata['x-xtm-one']``. On the way back in, the AgentSchema fields win and
the metadata only fills in what the spec has no room for — so hand-editing
``instructions`` or ``model`` in an exported file has the effect the user
expects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.models.agent_copy import (
    AGENT_BUNDLE_EXPORT_ONLY_FIELDS,
    AGENT_BUNDLE_PORTABLE_FIELDS,
)
from app.services.agentschema.spec import (
    AGENTSCHEMA_METADATA_NAMESPACE,
    AGENTSCHEMA_SPEC_VERSION,
    AGENTSCHEMA_SUPPORTED_KIND,
    AgentSchemaError,
)

#: Native bundle version the importer emits. Kept in step with the version
#: ``export_agent`` writes so both formats land on the same import path.
NATIVE_BUNDLE_VERSION = 3

#: Native fields the AgentSchema document itself carries, so the XTM One
#: metadata namespace must NOT shadow them: a stale copy in the extras would
#: silently win over a hand-edited ``instructions``, ``description`` or model
#: choice. Everything else in the bundle manifest round-trips through extras.
_DOCUMENT_OWNED_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "description",
        "persona",
        "llm_provider",
        "llm_model",
        "temperature",
        "reasoning_effort",
    }
)

#: XTM One built-in tool ids that have a genuine AgentSchema counterpart.
#:
#: Only ``code_interpreter`` does. AgentSchema v1.0's sole web tool is
#: ``WebSearchTool``, whose ``kind`` is the vendor-specific ``bing_search`` —
#: XTM One's ``web_fetch``/``browser`` retrieve and render a caller-supplied
#: URL rather than querying Bing, so mapping them onto it would misdescribe
#: what the agent can do. Every other built-in (``generate_image``,
#: ``work_items``, ``inspect_my_traces``, ...) is XTM One-specific. All of them
#: are reported as omitted and preserved in ``metadata``.
_BUILTIN_TOOL_KINDS: dict[str, str] = {
    "code_interpreter": "code_interpreter",
}

#: ``connection.name`` used for tools whose credentials live on the XTM One
#: instance. ``ReferenceConnection`` is the spec's "look this up by name on the
#: target platform" escape hatch, which is exactly the semantics of an MCP
#: server or knowledge base that must already exist wherever the document is
#: imported.
_MCP_CONNECTION_PREFIX = "xtm-one-mcp"
_KB_CONNECTION_NAME = "xtm-one-knowledge-base"

#: JSON Schema ``type`` -> AgentSchema ``Property.kind``. AgentSchema spells
#: JSON Schema's ``number`` as ``float`` and has no ``null`` kind.
_JSON_TYPE_TO_PROPERTY_KIND: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "float",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}

_PROPERTY_KIND_TO_JSON_TYPE: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "float": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


# --- Portability reporting -------------------------------------------------


@dataclass(slots=True)
class PortabilityEntry:
    """One agent capability that AgentSchema cannot carry as-is."""

    #: Stable machine key (the XTM One field or feature name).
    key: str
    #: Short human-readable label for the UI.
    label: str
    #: What happens to it, and why.
    detail: str
    #: ``"omitted"`` — absent from the AgentSchema fields entirely.
    #: ``"flattened"`` — content survives, but merged into a spec field and no
    #: longer a distinct, separately-managed entity.
    disposition: Literal["omitted", "flattened"] = "omitted"


@dataclass(slots=True)
class PortabilityReport:
    """What an AgentSchema export of a given agent would lose."""

    entries: list[PortabilityEntry] = field(default_factory=list)
    #: True when the omitted data is round-tripped through
    #: ``metadata['x-xtm-one']`` (always, today — kept explicit so the UI can
    #: state it rather than implying total loss).
    preserved_in_metadata: bool = True

    @property
    def omitted(self) -> list[PortabilityEntry]:
        return [e for e in self.entries if e.disposition == "omitted"]

    @property
    def flattened(self) -> list[PortabilityEntry]:
        return [e for e in self.entries if e.disposition == "flattened"]

    def field_keys(self) -> list[str]:
        """Machine keys, for audit-log details and telemetry."""
        return [e.key for e in self.entries]


def _add(
    report: PortabilityReport,
    key: str,
    label: str,
    detail: str,
    disposition: Literal["omitted", "flattened"] = "omitted",
) -> None:
    report.entries.append(
        PortabilityEntry(key=key, label=label, detail=detail, disposition=disposition)
    )


def portability_report(bundle: dict[str, Any]) -> PortabilityReport:
    """List what an AgentSchema export of ``bundle`` would not carry.

    Only reports what the agent *actually uses* — an agent with no handover
    configuration should not be warned about handover. Drives both the
    pre-export confirmation dialog and the audit-log breadcrumb that measures
    how often the gap bites in practice.
    """
    agent = bundle.get("agent") or {}
    report = PortabilityReport()

    if agent.get("guardrails"):
        _add(
            report,
            "guardrails",
            "Guardrails",
            "AgentSchema has no field for guardrails distinct from instructions, "
            "so they are appended to 'additionalInstructions'. They come back as "
            "guardrails on re-import into XTM One, but another platform will read "
            "them as plain extra instructions.",
            disposition="flattened",
        )

    handover_ids = agent.get("handover_agent_ids") or []
    if handover_ids or agent.get("handover_types") or agent.get("handover_conditions"):
        _add(
            report,
            "handover",
            "Handover routing",
            "Delegate/consult/transfer routing to peer agents, including its "
            "conditions, has no AgentSchema representation.",
        )

    memory_keys = [
        key
        for key in (
            "memory_short_term_enabled",
            "memory_medium_term_enabled",
            "memory_persistent_enabled",
            "memory_user_enabled",
        )
        # Every tier defaults to enabled, so only a *disabled* tier is a
        # setting worth warning about losing.
        if agent.get(key) is False
    ]
    if memory_keys:
        _add(
            report,
            "memory_tiers",
            "Memory tiers",
            "Short-term / medium-term / persistent / user memory toggles have no "
            "AgentSchema representation; the imported agent starts with all tiers "
            "enabled unless the XTM One metadata is present.",
        )

    assignments = bundle.get("assignments") or []
    if assignments:
        _add(
            report,
            "assignments",
            f"Assignments ({len(assignments)})",
            "Schedule / webhook / integration-event triggers, activation gates, "
            "human-in-the-loop and digest settings have no AgentSchema "
            "representation.",
        )

    providers = bundle.get("integration_providers") or []
    # Per-user template bindings are the mechanism every built-in agent uses for
    # OpenCTI / OpenAEV / OpenCRQ access, and an agent can have them with no
    # ``integration_providers`` entry at all — so they need naming here in their
    # own right, or a built-in agent exports with no integration warning while
    # losing every tool it has.
    template_providers = [
        str(p) for p in (agent.get("integration_template_bindings") or []) if p
    ]
    if providers or template_providers:
        names = sorted(
            {str(p.get("provider")) for p in providers if p.get("provider")}
            | set(template_providers)
        )
        _add(
            report,
            "integration_bindings",
            f"Integration bindings ({', '.join(names) or 'unnamed'})",
            "AgentSchema's Connection concept covers model and tool credentials "
            "only, not XTM One's business-integration binding model — including "
            "the per-user bindings that resolve each user's own platform "
            "credentials at runtime.",
        )

    if agent.get("human_in_the_loop"):
        _add(
            report,
            "human_in_the_loop",
            "Human in the loop",
            "Requiring a human to approve the agent's actions is an XTM One "
            "governance control with no AgentSchema representation; another "
            "platform would run the agent unsupervised.",
        )

    if bundle.get("prompts"):
        _add(
            report,
            "prompts",
            f"Prompts ({len(bundle['prompts'])})",
            "Reusable, separately-versioned prompt records have no AgentSchema "
            "representation, and are not flattened into the instructions.",
        )
    if bundle.get("skills"):
        _add(
            report,
            "skills",
            f"Skills ({len(bundle['skills'])})",
            "Reusable, separately-versioned skill records have no AgentSchema "
            "representation, and are not flattened into the instructions.",
        )
    if bundle.get("variables"):
        _add(
            report,
            "variables",
            f"Variables ({len(bundle['variables'])})",
            "Variable references inside the persona are exported verbatim; the "
            "variable records themselves have no AgentSchema representation.",
        )
    if bundle.get("knowledge_base_notes"):
        _add(
            report,
            "knowledge_base_config",
            "Knowledge base configuration",
            "Knowledge bases are exported as 'file_search' tool references by "
            "name only. Chunking, embedding and per-KB permission settings are "
            "not portable, and the referenced bases must already exist on the "
            "target instance.",
            disposition="flattened",
        )

    unsupported_builtins = [
        tool_id
        for tool_id in (agent.get("builtin_tool_ids") or [])
        if tool_id not in _BUILTIN_TOOL_KINDS
    ]
    if unsupported_builtins:
        _add(
            report,
            "builtin_tools",
            f"Built-in tools ({', '.join(sorted(unsupported_builtins))})",
            "Only 'code_interpreter' has an AgentSchema equivalent. AgentSchema's "
            "one web tool is Bing-specific ('bing_search'), which is not what "
            "XTM One's web_fetch/browser do, and the remaining built-ins are "
            "XTM One-specific.",
        )

    if agent.get("tool_deny_list"):
        _add(
            report,
            "tool_deny_list",
            "Tool deny list",
            "AgentSchema declares which tools an agent has, with no way to "
            "express a deny list layered over them.",
        )
    if agent.get("max_iterations") is not None:
        _add(
            report,
            "max_iterations",
            "Max iterations",
            "AgentSchema's ModelOptions has no agent-loop iteration ceiling.",
        )
    if agent.get("output_format") not in (None, "markdown", "json"):
        _add(
            report,
            "output_format",
            f"Output format ({agent.get('output_format')})",
            "AgentSchema can express a JSON output contract via 'outputSchema' "
            "but has no field for other rendering formats.",
        )
    if agent.get("disable_chat") or agent.get("hide_runs"):
        _add(
            report,
            "chat_visibility",
            "Chat/run visibility",
            "Whether the agent is chat-enabled or hides its runs is an XTM One "
            "product concern with no AgentSchema representation.",
        )
    coordination = agent.get("coordination_access")
    if (
        agent.get("work_item_owner_agent_ids")
        or agent.get("trace_supervision_target_agent_ids")
        or (
            isinstance(coordination, dict)
            and (coordination.get("read_agent_ids") or coordination.get("write_agent_ids"))
        )
    ):
        _add(
            report,
            "peer_agent_bindings",
            "Peer agent bindings",
            "Work-item ownership, trace-supervision targets and the agents a "
            "coordination access lists reference other XTM One agents, which "
            "AgentSchema cannot express.",
        )
    return report


# --- JSON Schema <-> PropertySchema ---------------------------------------


def _json_schema_to_property_schema(schema: Any) -> dict[str, Any] | None:
    """Convert a JSON Schema object to an AgentSchema ``PropertySchema``.

    AgentSchema does not embed JSON Schema: it declares a flat-ish
    ``PropertySchema`` of named ``Property`` records. This converts the shape
    XTM One stores (a standard ``{"type": "object", "properties": {...}}``)
    into the record form the spec's examples use.

    Nested object/array *shapes* are not carried — ``Property`` has no nested
    ``properties`` in the record form — so a nested field keeps its name, kind
    and description but loses its inner structure. Returns ``None`` when there
    is nothing convertible, since ``PropertySchema`` requires ``properties``.
    """
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return None

    required = schema.get("required")
    required_names = set(required) if isinstance(required, list) else set()

    converted: dict[str, Any] = {}
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        json_type = spec.get("type")
        if isinstance(json_type, list):
            # JSON Schema union types (["string", "null"]) collapse to the
            # first non-null member — AgentSchema Property.kind is single-valued.
            json_type = next((t for t in json_type if t != "null"), None)
        kind = _JSON_TYPE_TO_PROPERTY_KIND.get(str(json_type), "string")
        entry: dict[str, Any] = {"kind": kind}
        if spec.get("description"):
            entry["description"] = str(spec["description"])
        if str(name) in required_names:
            entry["required"] = True
        if "default" in spec:
            entry["default"] = spec["default"]
        if isinstance(spec.get("enum"), list) and spec["enum"]:
            entry["enumValues"] = list(spec["enum"])
        converted[str(name)] = entry

    if not converted:
        return None
    return {"properties": converted}


def _property_schema_to_json_schema(schema: Any) -> dict[str, Any] | None:
    """Convert an AgentSchema ``PropertySchema`` back to JSON Schema.

    Handles both spec-valid shapes for ``properties``: the record form
    (``{name: {...}}``) and the array form (``[{"name": ..., "kind": ...}]``).
    """
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")

    entries: list[tuple[str, dict[str, Any]]] = []
    if isinstance(properties, dict):
        entries = [
            (str(name), spec)
            for name, spec in properties.items()
            if isinstance(spec, dict)
        ]
    elif isinstance(properties, list):
        for spec in properties:
            if isinstance(spec, dict) and spec.get("name"):
                entries.append((str(spec["name"]), spec))
    if not entries:
        return None

    json_properties: dict[str, Any] = {}
    required: list[str] = []
    for name, spec in entries:
        json_type = _PROPERTY_KIND_TO_JSON_TYPE.get(str(spec.get("kind")), "string")
        prop: dict[str, Any] = {"type": json_type}
        if spec.get("description"):
            prop["description"] = str(spec["description"])
        if "default" in spec and spec["default"] is not None:
            prop["default"] = spec["default"]
        if isinstance(spec.get("enumValues"), list) and spec["enumValues"]:
            prop["enum"] = list(spec["enumValues"])
        json_properties[name] = prop
        if spec.get("required") is True:
            required.append(name)

    result: dict[str, Any] = {"type": "object", "properties": json_properties}
    if required:
        result["required"] = required
    return result


# --- Export: native bundle -> AgentSchema ---------------------------------


def _reference_connection(name: str, usage: str) -> dict[str, Any]:
    """A ``ReferenceConnection`` — "resolve this by name on the target platform"."""
    return {
        "kind": "reference",
        "name": name,
        "authenticationMode": "system",
        "usageDescription": usage,
    }


def _model_section(agent: dict[str, Any]) -> dict[str, Any]:
    """Build the ``model`` section.

    ``PromptAgent`` requires ``model`` and ``Model`` requires ``id``, so an
    agent with no model override (inheriting the platform default) still needs
    a value. ``"default"`` is emitted as an explicit, self-describing
    placeholder rather than inventing a concrete model id the source instance
    never chose.
    """
    model: dict[str, Any] = {"id": agent.get("llm_model") or "default"}
    if agent.get("llm_provider"):
        model["provider"] = str(agent["llm_provider"])

    # ``ModelOptions`` requires ``kind``; ``apiType: chat`` is what every
    # XTM One agent uses, so the options block is discriminated the same way.
    options: dict[str, Any] = {"kind": "chat"}
    if agent.get("temperature") is not None:
        options["temperature"] = agent["temperature"]
    # ``reasoning_effort`` has no first-class ModelOptions field, but the spec
    # declares an ``additionalProperties`` record for exactly this case.
    if agent.get("reasoning_effort"):
        options["additionalProperties"] = {
            "reasoning_effort": str(agent["reasoning_effort"])
        }
    if len(options) > 1:
        model["options"] = options
        model["apiType"] = "chat"
    return model


def _tool_entries(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the ``tools`` array.

    Uses the array form (rather than the record form) because it keeps tool
    order stable and lets two tools share a display name without one silently
    overwriting the other.
    """
    agent = bundle.get("agent") or {}
    tools: list[dict[str, Any]] = []

    for tool in bundle.get("custom_tools") or []:
        entry: dict[str, Any] = {
            "name": str(tool.get("name") or "custom_tool"),
            "kind": "function",
            "description": str(tool.get("description") or ""),
        }
        parameters = _json_schema_to_property_schema(tool.get("input_schema"))
        # ``FunctionTool`` requires ``parameters``; a no-argument tool still
        # needs the property present, so fall back to an empty record.
        entry["parameters"] = parameters or {"properties": {}}
        tools.append(entry)

    for server in bundle.get("mcp_servers") or []:
        name = str(server.get("name") or "mcp_server")
        entry = {
            "name": name,
            "kind": "mcp",
            "description": f"MCP server '{name}' ({server.get('transport') or 'stdio'})",
            "serverName": name,
            "connection": _reference_connection(
                f"{_MCP_CONNECTION_PREFIX}:{name}",
                "MCP server credentials are resolved on the importing platform; "
                "no secrets travel in this document.",
            ),
            # MCP tools are not whitelisted, so a gated agent pauses on each;
            # unset follows a default this document cannot see: "always".
            "approvalMode": {
                "kind": "never" if agent.get("approve_tool_calls") is False else "always"
            },
        }
        tools.append(entry)

    for note in bundle.get("knowledge_base_notes") or []:
        name = str(note.get("name") or "knowledge_base")
        tools.append(
            {
                "name": name,
                "kind": "file_search",
                "description": str(
                    note.get("description")
                    or f"XTM One knowledge base '{name}' (retrieval-augmented search)"
                ),
                "connection": _reference_connection(
                    _KB_CONNECTION_NAME,
                    "Knowledge bases are not carried in the document; the named "
                    "base must exist on the importing platform.",
                ),
                # The spec models the corpus as vector store ids. XTM One
                # exports the base *name*, the only identifier that means
                # anything on another instance.
                "vectorStoreIds": [name],
            }
        )

    for tool_id in agent.get("builtin_tool_ids") or []:
        kind = _BUILTIN_TOOL_KINDS.get(str(tool_id))
        if not kind:
            continue
        entry = {"name": str(tool_id), "kind": kind, "description": ""}
        if kind == "code_interpreter":
            # ``CodeInterpreterTool`` requires ``fileIds``; XTM One's
            # interpreter works on conversation attachments, not on files
            # pre-registered with the agent.
            entry["fileIds"] = []
        tools.append(entry)

    return tools


def _metadata_extras(bundle: dict[str, Any]) -> dict[str, Any]:
    """Everything the AgentSchema fields cannot carry, for the round-trip.

    Deliberately *not* the whole native bundle: the AgentSchema fields are the
    canonical, user-editable surface, and duplicating them here would let a
    stale copy silently win over a hand-edited ``instructions`` or ``model``.
    """
    agent = bundle.get("agent") or {}
    # Driven by the shared bundle manifest rather than a list maintained here,
    # so a field added to the native bundle survives an AgentSchema round-trip
    # instead of silently dropping out at this hop — which is the same class of
    # omission as in a bundle, one format further out. ``_DOCUMENT_OWNED_FIELDS`` are
    # the ones AgentSchema itself carries; everything else needs the XTM One
    # metadata namespace to survive.
    agent_extras = {
        key: agent[key]
        for key in (
            *AGENT_BUNDLE_PORTABLE_FIELDS,
            *AGENT_BUNDLE_EXPORT_ONLY_FIELDS,
        )
        if key not in _DOCUMENT_OWNED_FIELDS
        and key in agent
        and agent[key] not in (None, [], {})
    }

    extras: dict[str, Any] = {
        "spec_version": AGENTSCHEMA_SPEC_VERSION,
        "bundle_version": NATIVE_BUNDLE_VERSION,
        "agent": agent_extras,
    }
    # Only carry collections that have content — an export of a bare agent
    # should not be padded with a dozen empty lists.
    for key in (
        "assignments",
        "custom_tools",
        "mcp_servers",
        "skills",
        "prompts",
        "variables",
        "integration_providers",
        "knowledge_base_notes",
    ):
        if bundle.get(key):
            extras[key] = bundle[key]
    return extras


def build_agentschema_document(bundle: dict[str, Any]) -> dict[str, Any]:
    """Map a native export bundle to a ``PromptAgent`` document.

    Key order is meaningful: it is the order the YAML serializer emits, chosen
    so a human opening the file reads identity, then model, then tools, then
    the instructions.
    """
    agent = bundle.get("agent") or {}
    name = str(agent.get("name") or "").strip()
    if not name:
        raise AgentSchemaError("The agent has no name, which AgentSchema requires.")

    document: dict[str, Any] = {
        "kind": AGENTSCHEMA_SUPPORTED_KIND,
        "name": name,
        "displayName": name,
    }
    if agent.get("description"):
        document["description"] = str(agent["description"])

    metadata: dict[str, Any] = {}
    if agent.get("tags"):
        # ``metadata`` is where AgentSchema puts authors/tags per its own
        # description, so tags go in the standard place *and* in the XTM One
        # namespace (whence they are restored on re-import).
        metadata["tags"] = list(agent["tags"])
    metadata[AGENTSCHEMA_METADATA_NAMESPACE] = _metadata_extras(bundle)
    document["metadata"] = metadata

    document["model"] = _model_section(agent)

    tools = _tool_entries(bundle)
    if tools:
        document["tools"] = tools

    document["instructions"] = str(agent.get("persona") or "")
    if agent.get("guardrails"):
        document["additionalInstructions"] = str(agent["guardrails"])

    if agent.get("output_format") == "json":
        output_schema = _json_schema_to_property_schema(agent.get("output_schema"))
        if output_schema:
            document["outputSchema"] = output_schema

    return document


# --- Import: AgentSchema -> native bundle ---------------------------------


def _extras_from_metadata(document: dict[str, Any]) -> dict[str, Any]:
    """Read back the XTM One namespace, tolerating anything found there.

    The namespace is free-form by construction, and the document may have been
    hand-edited or produced by a different XTM One version, so a malformed
    value must degrade to "no extras" rather than fail the import.
    """
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    extras = metadata.get(AGENTSCHEMA_METADATA_NAMESPACE)
    if not isinstance(extras, dict):
        return {}
    return extras


def _model_from_document(document: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any]]:
    """Extract ``(llm_model, llm_provider, model options)``."""
    model = document.get("model")
    if isinstance(model, str):
        return model, None, {}
    if not isinstance(model, dict):
        return None, None, {}
    model_id = model.get("id")
    # ``"default"`` is the placeholder our own exporter writes for an agent
    # with no model override — reading it back as a literal model id would
    # pin the imported agent to a model that does not exist.
    if isinstance(model_id, str) and model_id.strip().lower() == "default":
        model_id = None
    options = model.get("options") if isinstance(model.get("options"), dict) else {}
    return (
        str(model_id) if model_id else None,
        str(model["provider"]) if model.get("provider") else None,
        options,
    )


def agentschema_to_native_bundle(
    document: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Convert a *validated* AgentSchema document into a native export bundle.

    Returns ``(bundle, warnings)``. Warnings call out references the target
    instance cannot satisfy (external MCP servers, knowledge bases, tool
    connections) — surfaced to the user rather than silently ignored.

    The caller must have run
    :func:`app.services.agentschema.spec.validate_import_document` first: this
    function assumes a spec-valid ``kind: prompt`` document.
    """
    warnings: list[str] = []
    extras = _extras_from_metadata(document)
    extra_agent = extras.get("agent") if isinstance(extras.get("agent"), dict) else {}

    name = str(document.get("displayName") or document.get("name") or "").strip()
    if not name:
        raise AgentSchemaError("The AgentSchema document has no usable agent name.")

    llm_model, llm_provider, model_options = _model_from_document(document)

    temperature = model_options.get("temperature")
    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
        temperature = None
    reasoning_effort = None
    additional = model_options.get("additionalProperties")
    if isinstance(additional, dict) and additional.get("reasoning_effort"):
        reasoning_effort = str(additional["reasoning_effort"])

    instructions = str(document.get("instructions") or "")
    additional_instructions = str(document.get("additionalInstructions") or "")

    # Guardrails round-trip out of the XTM One namespace when present. For a
    # document authored elsewhere, ``additionalInstructions`` is the closest
    # thing there is, so it lands in guardrails — the field whose content it
    # was in the other direction.
    guardrails = extra_agent.get("guardrails") or additional_instructions or None

    # An externally authored document may carry an outputSchema without any
    # XTM One output_format; a JSON contract implies JSON output.
    output_schema = extra_agent.get("output_schema") or _property_schema_to_json_schema(
        document.get("outputSchema")
    )
    output_format = extra_agent.get("output_format") or (
        "json" if output_schema else "markdown"
    )

    metadata = document.get("metadata")
    tags = extra_agent.get("tags")
    if not tags and isinstance(metadata, dict) and isinstance(metadata.get("tags"), list):
        tags = [str(tag) for tag in metadata["tags"]]

    agent: dict[str, Any] = {
        "name": name,
        "description": str(document.get("description") or "") or None,
        "icon": extra_agent.get("icon") or "Bot",
        "persona": instructions or None,
        "output_format": output_format,
        "output_schema": output_schema,
        "tags": list(tags) if isinstance(tags, list) else [],
        "config": extra_agent.get("config") or {},
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "temperature": temperature,
        "reasoning_effort": reasoning_effort,
        "max_iterations": extra_agent.get("max_iterations"),
        "guardrails": guardrails,
        "handover_types": extra_agent.get("handover_types") or {},
        "handover_conditions": extra_agent.get("handover_conditions") or {},
        "tool_deny_list": extra_agent.get("tool_deny_list") or [],
    }
    # Every other portable native field round-trips straight back out of the
    # XTM One namespace. Manifest-driven and additive: the explicitly computed
    # keys above are already in ``agent`` and stay authoritative (the document
    # wins over the extras for anything AgentSchema can express itself), while
    # a newly portable field needs no second edit here to survive.
    for key in AGENT_BUNDLE_PORTABLE_FIELDS:
        # ``builtin_tool_ids`` has its own merge below (round-tripped ids plus
        # any the document declares), including the type check a malformed
        # extras value needs.
        if key == "builtin_tool_ids":
            continue
        if key not in agent and key in extra_agent:
            agent[key] = extra_agent[key]

    # ``builtin_tool_ids`` is intentionally left out unless the XTM One
    # namespace supplies it: absent means "apply the platform default", which
    # the shared import service does. An externally authored document
    # declaring a ``code_interpreter`` tool adds to that default below.
    builtin_from_extras = extra_agent.get("builtin_tool_ids")

    # Round-tripped collections win over anything reconstructed from the
    # AgentSchema tool entries: they carry the full record (custom tool source,
    # MCP transport/command) that the spec representation cannot express. When
    # they are present there is nothing to reconstruct and nothing to warn
    # about, so the reconstruction is told to stay out of the way — otherwise
    # re-importing our own export would report a "tool with no implementation"
    # for a tool whose source is right there in the metadata.
    custom_tools = extras.get("custom_tools")
    mcp_servers = extras.get("mcp_servers")

    bundle_tools, bundle_servers, builtin_from_document, tool_warnings = _tools_to_bundle(
        document.get("tools"),
        skip_functions=isinstance(custom_tools, list),
        skip_mcp=isinstance(mcp_servers, list),
    )
    warnings.extend(tool_warnings)

    if isinstance(builtin_from_extras, list):
        builtin_ids = [str(item) for item in builtin_from_extras]
        for tool_id in builtin_from_document:
            if tool_id not in builtin_ids:
                builtin_ids.append(tool_id)
        agent["builtin_tool_ids"] = builtin_ids
    elif builtin_from_document:
        agent["builtin_tool_ids"] = builtin_from_document

    bundle: dict[str, Any] = {
        "version": NATIVE_BUNDLE_VERSION,
        "export_type": "agent",
        "agent": agent,
        "assignments": extras.get("assignments") or [],
        "custom_tools": custom_tools if isinstance(custom_tools, list) else bundle_tools,
        "mcp_servers": mcp_servers if isinstance(mcp_servers, list) else bundle_servers,
        "skills": extras.get("skills") or [],
        "prompts": extras.get("prompts") or [],
        "variables": extras.get("variables") or [],
        "integration_providers": extras.get("integration_providers") or [],
        "knowledge_base_notes": extras.get("knowledge_base_notes") or [],
    }
    return bundle, warnings


def _tools_to_bundle(
    tools: Any,
    *,
    skip_functions: bool = False,
    skip_mcp: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    """Split AgentSchema tool entries into native bundle collections.

    Returns ``(custom_tools, mcp_servers, builtin_tool_ids, warnings)``.

    A ``function`` tool from another platform has a *signature* but no body —
    XTM One cannot execute it — so it is imported as a disabled-by-nature stub
    (empty ``js_function``) and reported, which is more useful than dropping a
    tool the agent's instructions refer to by name.

    ``skip_functions`` / ``skip_mcp`` suppress that reconstruction (and its
    warnings) for the collections the caller is restoring from the XTM One
    metadata namespace, which hold the complete records.
    """
    custom_tools: list[dict[str, Any]] = []
    mcp_servers: list[dict[str, Any]] = []
    builtin_ids: list[str] = []
    warnings: list[str] = []

    if not isinstance(tools, list):
        return custom_tools, mcp_servers, builtin_ids, warnings

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        kind = str(tool.get("kind") or "")
        name = str(tool.get("name") or kind or "tool")
        description = str(tool.get("description") or "")

        if kind == "function":
            if skip_functions:
                continue
            custom_tools.append(
                {
                    "name": name,
                    "description": description,
                    "input_schema": _property_schema_to_json_schema(
                        tool.get("parameters")
                    ),
                    "js_function": "",
                    "tags": ["agentschema-import"],
                }
            )
            warnings.append(
                f"Function tool '{name}' was imported without an implementation — "
                "AgentSchema declares a tool's signature, not its code. Add the "
                "function body before enabling it."
            )
        elif kind == "mcp":
            if skip_mcp:
                continue
            server_name = str(tool.get("serverName") or name)
            mcp_servers.append(
                {
                    "name": server_name,
                    # No transport details travel in an AgentSchema document,
                    # so the record is a placeholder the user completes.
                    "transport": "stdio",
                    "command": None,
                    "args": [],
                    "url": None,
                    "tags": ["agentschema-import"],
                }
            )
            warnings.append(
                f"MCP server '{server_name}' was imported as a placeholder: "
                "AgentSchema carries a connection reference, not the server "
                "address or credentials. Configure it before use."
            )
        elif kind == "file_search":
            stores = tool.get("vectorStoreIds")
            label = ", ".join(str(s) for s in stores) if isinstance(stores, list) else name
            warnings.append(
                f"File search tool '{name}' references corpora ({label}) that are "
                "not part of the document. Bind the agent to a knowledge base on "
                "this instance to restore retrieval."
            )
        elif kind in _BUILTIN_TOOL_KINDS.values():
            # Reverse the built-in mapping (currently only code_interpreter).
            for tool_id, mapped in _BUILTIN_TOOL_KINDS.items():
                if mapped == kind and tool_id not in builtin_ids:
                    builtin_ids.append(tool_id)
        else:
            warnings.append(
                f"Tool '{name}' declares kind '{kind}', which XTM One has no "
                "equivalent for; it was not imported."
            )

    return custom_tools, mcp_servers, builtin_ids, warnings
