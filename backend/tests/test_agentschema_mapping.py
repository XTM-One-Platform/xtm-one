"""Tests for the XTM One <-> Microsoft AgentSchema field mapping.

The contract being pinned here:

- an export is a *spec-valid* ``PromptAgent`` document (asserted against the
  vendored official schema, not against our own reading of it);
- an XTM One -> AgentSchema -> XTM One round-trip loses nothing, because what
  the spec cannot express is preserved under ``metadata['x-xtm-one']``;
- the AgentSchema fields stay the canonical surface: hand-editing
  ``instructions`` or ``model`` in an exported file takes effect on re-import,
  rather than being overruled by the preserved metadata;
- nothing is dropped *silently* — anything not carried is reported by
  :func:`portability_report`, and anything unresolvable on this instance comes
  back as an import warning.
"""

from __future__ import annotations

import json

import pytest

from app.services.agentschema import (
    AGENTSCHEMA_METADATA_NAMESPACE,
    AgentSchemaError,
    build_agentschema_export,
    portability_report,
    prepare_agent_import,
    schema_problems,
)
from app.services.agentschema.detect import (
    AGENTSCHEMA_FORMAT,
    NATIVE_FORMAT,
    detect_format,
    validate_native_bundle,
)
from app.services.agentschema.mapping import build_agentschema_document


def _bundle(**agent_overrides) -> dict:
    """A native ``version: 3`` bundle exercising every mapped field."""
    agent = {
        "name": "Threat Analyst",
        "description": "Analyses CTI reports",
        "icon": "Shield",
        "persona": "You are a threat analyst.\nUse *markdown* & be concise.",
        "output_format": "json",
        "output_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["benign", "malicious"]},
                "score": {"type": "number", "description": "0..1"},
            },
            "required": ["verdict"],
        },
        "tags": ["cti", "soc"],
        "config": {"threshold": 0.8},
        "llm_provider": "anthropic",
        "llm_model": "claude-opus-5",
        "temperature": 0.3,
        "reasoning_effort": "high",
        "max_iterations": 12,
        "guardrails": "Never disclose internal hostnames.",
        "memory_short_term_enabled": True,
        "memory_medium_term_enabled": False,
        "memory_persistent_enabled": True,
        "memory_user_enabled": True,
        "disable_chat": False,
        "handover_types": {"peer-id": "delegate"},
        "handover_conditions": {"peer-id": "when unsure"},
        "work_item_owner_agent_ids": [],
        "trace_supervision_target_agent_ids": [],
        "builtin_tool_ids": ["code_interpreter", "web_fetch", "inspect_my_traces"],
        "tool_deny_list": ["dangerous_tool"],
        # Per-user integration access, tool scoping and the governance
        # flags must survive an AgentSchema round-trip through the XTM One
        # metadata namespace, exactly like the fields above.
        "integration_template_bindings": ["opencti"],
        "integration_allowed_tools": {"opencti": ["list_reports"]},
        "handover_prompt": "Hand over when the question is not CTI.",
        "supported_intents": {"cti.dashboard": {"cti": 0}},
        "supported_verticals": ["cti"],
        "human_in_the_loop": True,
        "human_in_the_loop_prompt": "Confirm before writing.",
        "hide_runs": True,
        "web_suggested_prompts": ["Summarise last night's reports"],
    }
    agent.update(agent_overrides)
    return {
        "version": 3,
        "export_type": "agent",
        "exported_at": "2026-07-30T10:00:00+00:00",
        "agent": agent,
        "assignments": [
            {
                "name": "Nightly sweep",
                "trigger_type": "schedule",
                "trigger_config": {"cron": "0 2 * * *"},
                "prompt": "Review yesterday's reports",
            }
        ],
        "custom_tools": [
            {
                "name": "lookup_ioc",
                "description": "Look up an indicator",
                "icon_url": None,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "ioc": {"type": "string", "description": "The indicator"},
                        "deep": {"type": "boolean", "default": False},
                    },
                    "required": ["ioc"],
                },
                "js_function": "return lookup($ioc);",
                "tags": [],
            }
        ],
        "mcp_servers": [
            {
                "name": "filesystem",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "server-filesystem"],
                "url": None,
                "tags": [],
            }
        ],
        "integration_providers": [
            {"provider": "github", "name": "gh", "type": "github"}
        ],
        "skills": [
            {
                "name": "triage",
                "description": "Triage guidance",
                "url": None,
                "content": "Steps...",
                "compatibility": None,
                "tags": [],
            }
        ],
        "prompts": [
            {"name": "summary", "description": None, "content": "Summarize", "tags": []}
        ],
        "knowledge_base_notes": [
            {"name": "CTI corpus", "description": "Past reports", "note": "not included"}
        ],
        "variables": [
            {
                "id": "var-1",
                "name": "API_HOST",
                "key": "API_HOST",
                "description": None,
                "tags": [],
                "is_secret": False,
                "value": "https://example.test",
            }
        ],
    }


# --- Export ----------------------------------------------------------------


def test_export_validates_against_the_official_schema():
    """The headline guarantee: our output satisfies Microsoft's own schema."""
    _text, document = build_agentschema_export(_bundle())
    assert schema_problems(document) == []
    assert document["kind"] == "prompt"


def test_export_maps_the_direct_fields():
    document = build_agentschema_document(_bundle())
    assert document["name"] == "Threat Analyst"
    assert document["displayName"] == "Threat Analyst"
    assert document["description"] == "Analyses CTI reports"
    assert document["instructions"].startswith("You are a threat analyst.")
    assert document["model"]["id"] == "claude-opus-5"
    assert document["model"]["provider"] == "anthropic"
    assert document["model"]["options"]["temperature"] == 0.3
    # ``reasoning_effort`` has no first-class ModelOptions field; the spec's
    # own ``additionalProperties`` record is where it belongs.
    assert (
        document["model"]["options"]["additionalProperties"]["reasoning_effort"] == "high"
    )
    # Guardrails are distinct in our model but have no distinct spec field.
    assert document["additionalInstructions"] == "Never disclose internal hostnames."


def test_mcp_approval_mode_follows_the_agent_approval_switch():
    """An agent with the gate explicitly off declares "never"."""
    bundle = _bundle()
    bundle["agent"]["approve_tool_calls"] = False

    document = build_agentschema_document(bundle)
    by_kind = {tool["kind"]: tool for tool in document["tools"]}

    assert by_kind["mcp"]["approvalMode"] == {"kind": "never"}


def test_export_maps_tools_to_their_spec_kinds():
    document = build_agentschema_document(_bundle())
    by_kind = {tool["kind"]: tool for tool in document["tools"]}

    assert by_kind["function"]["name"] == "lookup_ioc"
    # JSON Schema is translated into a PropertySchema, not passed through.
    parameters = by_kind["function"]["parameters"]["properties"]
    assert parameters["ioc"] == {
        "kind": "string",
        "description": "The indicator",
        "required": True,
    }
    assert parameters["deep"]["kind"] == "boolean"

    assert by_kind["mcp"]["serverName"] == "filesystem"
    assert by_kind["mcp"]["connection"]["kind"] == "reference"
    # "always": an agent with no approve_tool_calls of its own follows an
    # install default the document cannot see, and MCP tools are not on the
    # HITL whitelist (FR-022), so understating the pauses is the unsafe side.
    assert by_kind["mcp"]["approvalMode"] == {"kind": "always"}

    assert by_kind["file_search"]["vectorStoreIds"] == ["CTI corpus"]
    assert by_kind["code_interpreter"]["fileIds"] == []

    # XTM One built-ins with no honest equivalent are not invented into
    # existence — AgentSchema's only web tool is Bing-specific.
    assert "bing_search" not in by_kind
    assert "web_fetch" not in {tool["name"] for tool in document["tools"]}


def test_export_translates_output_schema_only_for_json_output():
    document = build_agentschema_document(_bundle())
    properties = document["outputSchema"]["properties"]
    assert properties["verdict"]["required"] is True
    assert properties["verdict"]["enumValues"] == ["benign", "malicious"]
    # JSON Schema's ``number`` is spelled ``float`` in AgentSchema.
    assert properties["score"]["kind"] == "float"

    markdown = build_agentschema_document(_bundle(output_format="markdown"))
    assert "outputSchema" not in markdown


def test_export_uses_an_explicit_placeholder_for_an_inherited_model():
    """``PromptAgent`` requires ``model.id``, so "no override" needs a value.

    A self-describing ``default`` beats inventing a concrete model id the
    source instance never chose (and it is read back as "no override").
    """
    document = build_agentschema_document(_bundle(llm_model=None, llm_provider=None))
    assert document["model"]["id"] == "default"
    assert schema_problems(document) == []


def test_export_requires_a_name():
    with pytest.raises(AgentSchemaError, match="no name"):
        build_agentschema_document(_bundle(name=""))


def test_export_omits_the_options_block_when_there_is_nothing_to_put_in_it():
    document = build_agentschema_document(
        _bundle(temperature=None, reasoning_effort=None)
    )
    assert "options" not in document["model"]
    assert schema_problems(document) == []


# --- Portability report ----------------------------------------------------


def test_portability_report_lists_what_is_lost_and_what_is_flattened():
    report = portability_report(_bundle())
    keys = report.field_keys()

    for expected in (
        "handover",
        "memory_tiers",
        "assignments",
        "integration_bindings",
        "prompts",
        "skills",
        "variables",
        "builtin_tools",
        "tool_deny_list",
        "max_iterations",
        # An agent whose access is per-user (no integration_bindings at all)
        # must still be told its integration access cannot travel.
        "human_in_the_loop",
    ):
        assert expected in keys, f"{expected} should be reported as non-portable"

    integration_entry = next(
        entry for entry in report.entries if entry.key == "integration_bindings"
    )
    assert "opencti" in integration_entry.label

    assert {entry.key for entry in report.flattened} == {
        "guardrails",
        "knowledge_base_config",
    }
    assert report.preserved_in_metadata is True
    # Every entry explains itself — the UI renders ``detail`` verbatim.
    assert all(entry.detail for entry in report.entries)


def test_portability_report_only_mentions_features_the_agent_uses():
    """A plain agent must not be warned about features it does not have."""
    bare = {
        "version": 3,
        "export_type": "agent",
        "agent": {
            "name": "Simple",
            "persona": "Be helpful.",
            "llm_model": "gpt-4o",
            "builtin_tool_ids": ["code_interpreter"],
        },
    }
    assert portability_report(bare).field_keys() == []


def test_portability_report_ignores_memory_tiers_left_at_their_default():
    """Every tier defaults to enabled; only a *disabled* tier is a lost setting."""
    all_enabled = _bundle(memory_medium_term_enabled=True)
    assert "memory_tiers" not in portability_report(all_enabled).field_keys()


# --- Round-trip ------------------------------------------------------------


def test_round_trip_preserves_every_agent_field():
    text, _document = build_agentschema_export(_bundle())
    prepared = prepare_agent_import(text.encode(), filename="agent.agent.yaml")

    assert prepared.format == AGENTSCHEMA_FORMAT
    original = _bundle()["agent"]
    restored = prepared.bundle["agent"]
    for key, value in original.items():
        # Peer-agent references are cleared on import by design (the ids belong
        # to another instance), so they are not part of the round-trip contract.
        if key in ("work_item_owner_agent_ids", "trace_supervision_target_agent_ids"):
            continue
        assert restored.get(key) == value, f"{key} did not survive the round-trip"


def test_round_trip_preserves_every_collection():
    text, _document = build_agentschema_export(_bundle())
    prepared = prepare_agent_import(text.encode())
    source = _bundle()
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
        assert prepared.bundle[key] == source[key], f"{key} did not survive"


def test_round_trip_warns_only_about_what_is_genuinely_unresolved():
    """Re-importing our own export must not cry wolf.

    The custom-tool source and MCP server records come back from the metadata,
    so warning about "a tool with no implementation" would be actively
    misleading. The knowledge base warning stays: agent imports never re-bind
    knowledge bases (they are platform-specific), so retrieval really does need
    the user's attention.
    """
    text, _document = build_agentschema_export(_bundle())
    prepared = prepare_agent_import(text.encode())

    assert prepared.notes == []
    assert len(prepared.warnings) == 1
    assert "CTI corpus" in prepared.warnings[0]
    joined = " ".join(prepared.warnings)
    assert "without an implementation" not in joined
    assert "placeholder" not in joined


def test_hand_edited_spec_fields_win_over_the_preserved_metadata():
    """The AgentSchema fields are the canonical, user-editable surface.

    Preserving a whole copy of the bundle in ``metadata`` would make an edit to
    ``instructions`` silently ineffective — the exact footgun this asserts is
    absent.
    """
    _text, document = build_agentschema_export(_bundle())
    document["instructions"] = "Rewritten by hand."
    document["model"]["id"] = "gpt-4o"
    document["name"] = "Renamed Agent"
    document["displayName"] = "Renamed Agent"

    prepared = prepare_agent_import(json.dumps(document).encode())
    agent = prepared.bundle["agent"]
    assert agent["persona"] == "Rewritten by hand."
    assert agent["llm_model"] == "gpt-4o"
    assert agent["name"] == "Renamed Agent"
    # ...while the non-portable fields still come back from the metadata.
    assert agent["handover_types"] == {"peer-id": "delegate"}
    assert agent["memory_medium_term_enabled"] is False


def test_metadata_namespace_keeps_the_document_spec_valid():
    _text, document = build_agentschema_export(_bundle())
    assert AGENTSCHEMA_METADATA_NAMESPACE in document["metadata"]
    # ``metadata`` is ``RecordUnknown`` — free-form by design, so the extension
    # cannot make the document invalid.
    assert schema_problems(document) == []
    # Tags also go in the standard place AgentSchema documents them.
    assert document["metadata"]["tags"] == ["cti", "soc"]


def test_import_tolerates_a_corrupted_metadata_namespace():
    """A hand-mangled namespace degrades to "no extras", never to a failure."""
    _text, document = build_agentschema_export(_bundle())
    document["metadata"][AGENTSCHEMA_METADATA_NAMESPACE] = "not-an-object"
    prepared = prepare_agent_import(json.dumps(document).encode())
    assert prepared.bundle["agent"]["persona"].startswith("You are a threat analyst.")
    assert prepared.bundle["assignments"] == []


# --- Importing a foreign document ------------------------------------------


FOREIGN_DOCUMENT = """
kind: prompt
name: External Assistant
description: Authored on another platform
model:
  id: gpt-4o
  provider: openai
  options:
    kind: chat
    temperature: 0.7
instructions: You are a helpful assistant.
additionalInstructions: Never reveal system details.
tools:
  - name: get_weather
    kind: function
    description: Look up the weather
    parameters:
      properties:
        city:
          kind: string
          required: true
  - name: corporate_docs
    kind: file_search
    connection:
      kind: reference
      name: docs
      authenticationMode: system
    vectorStoreIds: [docs-store]
  - name: remote_tools
    kind: mcp
    serverName: remote-tools
    connection:
      kind: reference
      name: remote
      authenticationMode: system
    approvalMode:
      kind: never
  - name: bing
    kind: bing_search
    connection:
      kind: reference
      name: bing
      authenticationMode: system
outputSchema:
  properties:
    answer:
      kind: string
      required: true
"""


def test_foreign_document_maps_onto_the_native_bundle():
    prepared = prepare_agent_import(FOREIGN_DOCUMENT.encode(), filename="external.yaml")
    assert prepared.format == AGENTSCHEMA_FORMAT

    agent = prepared.bundle["agent"]
    assert agent["name"] == "External Assistant"
    assert agent["persona"] == "You are a helpful assistant."
    assert agent["llm_model"] == "gpt-4o"
    assert agent["llm_provider"] == "openai"
    assert agent["temperature"] == 0.7
    # With no XTM One metadata to restore, ``additionalInstructions`` is the
    # closest thing to guardrails there is.
    assert agent["guardrails"] == "Never reveal system details."
    # An output contract implies JSON output even with no XTM One hint.
    assert agent["output_format"] == "json"
    assert agent["output_schema"] == {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    # ``builtin_tool_ids`` is left unset so the import pipeline applies the
    # platform default rather than an empty list.
    assert "builtin_tool_ids" not in agent


def test_foreign_document_warns_about_everything_it_cannot_resolve():
    prepared = prepare_agent_import(FOREIGN_DOCUMENT.encode())
    joined = " ".join(prepared.warnings)

    # A function tool from elsewhere has a signature but no body.
    assert "get_weather" in joined and "without an implementation" in joined
    # An MCP connection reference is not a server address.
    assert "remote-tools" in joined and "placeholder" in joined
    # A vector store on another platform is not a knowledge base here.
    assert "corporate_docs" in joined
    # A tool kind we have no equivalent for is reported, not quietly dropped.
    assert "bing_search" in joined

    stub = prepared.bundle["custom_tools"][0]
    assert stub["name"] == "get_weather"
    assert stub["js_function"] == ""
    assert stub["input_schema"] == {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }


def test_foreign_document_declaring_code_interpreter_gets_that_builtin():
    document = """
    kind: prompt
    name: Analyst
    model: gpt-4o
    instructions: Analyse things.
    tools:
      - name: code
        kind: code_interpreter
        fileIds: []
    """
    prepared = prepare_agent_import(document.encode())
    assert prepared.bundle["agent"]["builtin_tool_ids"] == ["code_interpreter"]


# --- Detection -------------------------------------------------------------


def test_detects_the_native_bundle():
    assert detect_format(_bundle()) == NATIVE_FORMAT
    # v1/v2 bundles and hand-edited files that dropped ``export_type``.
    assert detect_format({"version": 1, "agent": {"name": "n"}}) == NATIVE_FORMAT


def test_detects_an_agentschema_document():
    assert detect_format({"kind": "prompt", "name": "n", "model": "m"}) == AGENTSCHEMA_FORMAT
    # No ``kind`` — the shape real-world documents actually have.
    assert detect_format({"name": "n", "instructions": "hi"}) == AGENTSCHEMA_FORMAT
    # ``hosted``/``workflow`` are detected here and rejected at validation, so
    # the user gets "we can't import this kind" instead of "unknown file".
    assert detect_format({"kind": "hosted", "name": "n"}) == AGENTSCHEMA_FORMAT


def test_detection_rejects_an_unrecognized_file_with_both_formats_explained():
    with pytest.raises(AgentSchemaError) as excinfo:
        detect_format({"hello": "world", "foo": 1})
    message = str(excinfo.value)
    assert "export_type: agent" in message
    assert "kind: prompt" in message
    assert "hello" in message  # the fields it actually found


def test_detection_refuses_to_guess_when_a_file_matches_both():
    with pytest.raises(AgentSchemaError, match="both supported formats"):
        detect_format(
            {"export_type": "agent", "agent": {"name": "n"}, "kind": "prompt", "name": "n"}
        )


def test_end_to_end_import_of_a_native_bundle():
    prepared = prepare_agent_import(json.dumps(_bundle()).encode(), filename="agent.json")
    assert prepared.format == NATIVE_FORMAT
    # Passed through untouched — the native path is not re-mapped.
    assert prepared.bundle == _bundle()


# --- Native bundle verification loop ---------------------------------------


def test_native_validation_reports_every_problem_at_once():
    with pytest.raises(AgentSchemaError) as excinfo:
        validate_native_bundle(
            {"version": 9, "export_type": "agent", "agent": {}, "assignments": "nope"}
        )
    problems = excinfo.value.problems
    assert any("unsupported export version 9" in problem for problem in problems)
    assert any("agent/name" in problem for problem in problems)
    assert any("assignments: expected a list" in problem for problem in problems)


def test_native_validation_rejects_a_non_agent_export_type():
    """An integration or tool bundle must not be half-imported as an agent."""
    with pytest.raises(AgentSchemaError) as excinfo:
        validate_native_bundle(
            {"version": 3, "export_type": "integration", "agent": {"name": "n"}}
        )
    assert any(
        "unsupported export type" in problem for problem in excinfo.value.problems
    )


def test_native_validation_notes_an_assumed_export_type():
    notes = validate_native_bundle({"version": 3, "agent": {"name": "n"}})
    assert any("read as 'agent'" in note for note in notes)


# --- Size limits -----------------------------------------------------------


def test_agentschema_documents_have_a_tighter_size_ceiling_than_bundles():
    """A single declaration does not need the archive-sized allowance."""
    from app.services.agentschema import AGENTSCHEMA_MAX_BYTES, NATIVE_BUNDLE_MAX_BYTES

    assert AGENTSCHEMA_MAX_BYTES < NATIVE_BUNDLE_MAX_BYTES

    padded = dict(
        kind="prompt",
        name="Big",
        model={"id": "m"},
        instructions="x" * (AGENTSCHEMA_MAX_BYTES + 1024),
    )
    with pytest.raises(AgentSchemaError, match=r"over the .* limit"):
        prepare_agent_import(json.dumps(padded).encode())
