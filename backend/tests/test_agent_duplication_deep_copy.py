"""Regression tests for the agent duplication deep-copy contract.

Duplicating an agent must clone every JSON-mapped column, so that subsequent
edits on the duplicate never leak back into the source agent. These tests drive
``build_agent_copy_kwargs`` — the shared manifest-driven helper the
``/agents/{id}/duplicate`` handler builds its kwargs from — rather than
replaying the handler's field copies by hand: a hand-written replay is what let
the handler and its test drift apart until a duplicated OpenCTI agent silently
lost every one of its tools.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.models.agent_copy import AGENT_COPY_FIELDS, build_agent_copy_kwargs

# The JSON-shaped fields these tests care about; every other manifest field is
# filled with None by ``_build_source``.
_NESTED_FIELDS = {
    # Nested dict → list[str]: classic shared-reference foot-gun
    "integration_allowed_tools": {
        "github": ["get_repo", "list_issues"],
        "microsoft": ["list_emails"],
    },
    # Nested dict → dict
    "integration_element_bindings": {
        "github": {"repos": ["filigran/filigran-copilot"]},
    },
    # Nested dict → list[str]
    "mcp_allowed_tools": {
        "uuid-server-1": ["fetch_url"],
    },
    # Pipeline refs with nested args
    "prepare_tool_ref": {
        "source": "builtin",
        "tool": "stix_bundle_validator",
        "args": {"strict": True, "tags": ["cti"]},
    },
    "finalize_tool_ref": {
        "source": "integration",
        "tool": "send_message",
        "args": {"channel": "alerts"},
    },
    # Routing fields
    "supported_intents": {
        "cti.extract": {"cti": 0, "iam": 3},
        "cti.harvest": {"cti": 5},
    },
    "supported_verticals": ["cti", "fimi"],
    # Flat dicts that can still hold lists/dicts as values
    "handover_types": {"transfer": ["xtm-one-assistant"]},
    "handover_conditions": {"transfer": {"min_confidence": 0.7}},
    # Other JSON-shaped fields
    "output_schema": {"type": "object", "properties": {"alerts": {"type": "array"}}},
    "config": {"flags": {"experimental": True}},
    "integration_bindings": {"github": {"installation_id": "abc"}},
    "integration_template_bindings": ["opencti"],
    "tool_deny_list": ["delete_opencti_entity"],
    "channel_config": {"web": {"streaming": True}},
    "guardrails": {"deny": ["self-harm"]},
}


def _build_source(**overrides) -> SimpleNamespace:
    """A stand-in for an ORM `Agent` row exposing every manifest field."""
    values = {name: None for name in AGENT_COPY_FIELDS}
    values.update(_NESTED_FIELDS)
    values.update(overrides)
    return SimpleNamespace(**values)


def _duplicate(source: SimpleNamespace) -> SimpleNamespace:
    """The copy the handler would build, via the shared manifest helper."""
    return SimpleNamespace(**build_agent_copy_kwargs(source))


def test_duplicate_values_compare_equal_to_source():
    src = _build_source()
    dup = _duplicate(src)
    for attr in AGENT_COPY_FIELDS:
        assert getattr(dup, attr) == getattr(src, attr), attr


def test_nested_list_mutations_on_duplicate_do_not_affect_source():
    src = _build_source()
    dup = _duplicate(src)

    dup.integration_allowed_tools["github"].append("delete_repo")
    dup.integration_allowed_tools["microsoft"] = None
    dup.mcp_allowed_tools["uuid-server-1"].append("write_file")
    dup.tool_deny_list.clear()
    dup.integration_template_bindings.append("openaev")

    assert src.integration_allowed_tools["github"] == ["get_repo", "list_issues"]
    assert src.integration_allowed_tools["microsoft"] == ["list_emails"]
    assert src.mcp_allowed_tools["uuid-server-1"] == ["fetch_url"]
    assert src.tool_deny_list == ["delete_opencti_entity"]
    assert src.integration_template_bindings == ["opencti"]


def test_nested_dict_mutations_on_duplicate_do_not_affect_source():
    src = _build_source()
    dup = _duplicate(src)

    dup.integration_element_bindings["github"]["repos"].append("filigran/xtm-hub")
    dup.handover_conditions["transfer"]["min_confidence"] = 0.99
    dup.prepare_tool_ref["args"]["tags"].append("aev")
    dup.config["flags"]["experimental"] = False
    dup.output_schema["properties"]["alerts"]["type"] = "string"

    assert src.integration_element_bindings["github"]["repos"] == [
        "filigran/filigran-copilot"
    ]
    assert src.handover_conditions["transfer"]["min_confidence"] == 0.7
    assert src.prepare_tool_ref["args"]["tags"] == ["cti"]
    assert src.config["flags"]["experimental"] is True
    assert src.output_schema["properties"]["alerts"]["type"] == "array"


def test_flat_list_mutations_on_duplicate_do_not_affect_source():
    src = _build_source()
    dup = _duplicate(src)

    dup.supported_intents["cti.review"] = {"cti": 0}
    dup.supported_verticals.append("iam")

    assert src.supported_intents == {
        "cti.extract": {"cti": 0, "iam": 3},
        "cti.harvest": {"cti": 5},
    }
    assert src.supported_verticals == ["cti", "fimi"]


def test_replacing_top_level_dict_on_duplicate_does_not_affect_source():
    src = _build_source()
    dup = _duplicate(src)

    dup.handover_types = {"consult": ["opencti-assistant"]}
    dup.guardrails = None

    assert src.handover_types == {"transfer": ["xtm-one-assistant"]}
    assert src.guardrails == {"deny": ["self-harm"]}


def test_none_source_values_pass_through_as_none():
    """A NULL column stays NULL on the copy rather than becoming ``[]``/``{}``.

    NULL is a meaningful state on several of these columns (a NULL
    ``available_channel_types`` means "every channel" on a legacy row), every
    read site already treats NULL and empty alike, and a copy that reshaped
    values would no longer be a faithful duplicate.
    """
    src = SimpleNamespace(**{name: None for name in AGENT_COPY_FIELDS})
    dup = _duplicate(src)

    for attr in AGENT_COPY_FIELDS:
        assert getattr(dup, attr) is None, attr
