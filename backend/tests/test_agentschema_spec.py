"""Tests for the vendored Microsoft AgentSchema spec layer.

Covers the parts that must stay true about the *specification* itself:

- the vendored schema files are complete, resolvable offline, and the pinned
  upstream commit in code matches the one in ``NOTICE.md``;
- the safe parser refuses what it must (alias bombs, non-UTF-8, non-object
  roots, runaway nesting) and accepts what it must (markdown text that merely
  *looks* like YAML syntax);
- normalization is lenient in exactly the ways real-world documents need,
  including Microsoft's own published example;
- validation reports per-field problems, and rejects the two agent kinds
  XTM One has no counterpart for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.services.agentschema import spec
from app.services.agentschema.spec import (
    AGENTSCHEMA_SUPPORTED_KIND,
    AGENTSCHEMA_UPSTREAM_COMMIT,
    AgentSchemaError,
    normalize_document,
    parse_document,
    schema_problems,
    validate_import_document,
)

SCHEMA_DIR = Path(spec.__file__).parent / "schemas"


def _minimal_document(**overrides) -> dict:
    document = {
        "kind": "prompt",
        "name": "test-agent",
        "model": {"id": "gpt-4o"},
        "instructions": "Be helpful.",
    }
    document.update(overrides)
    return document


# --- Vendor integrity ------------------------------------------------------


def test_vendored_schemas_are_present_and_parse():
    files = sorted(path.name for path in SCHEMA_DIR.glob("*.yaml"))
    assert "PromptAgent.yaml" in files
    assert "AgentDefinition.yaml" in files
    # The whole v1.0 directory is vendored so every ``$ref`` resolves locally.
    assert len(files) > 40
    for name in files:
        assert yaml.safe_load((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def test_notice_pins_the_same_commit_as_the_code():
    """The pin in ``spec.py`` and the one in ``NOTICE.md`` must not drift.

    They are updated by hand after running
    ``scripts/refresh_agentschema_vendor.py``, and a stale notice would
    misreport which upstream revision the platform actually validates against.
    """
    notice = (SCHEMA_DIR / "NOTICE.md").read_text(encoding="utf-8")
    assert re.search(rf"\b{AGENTSCHEMA_UPSTREAM_COMMIT}\b", notice), (
        "NOTICE.md does not mention AGENTSCHEMA_UPSTREAM_COMMIT "
        f"({AGENTSCHEMA_UPSTREAM_COMMIT})"
    )


def test_refs_resolve_offline():
    """A valid document validates with no network access.

    ``$ref: Model.yaml`` and friends are relative URIs; if the local registry
    were not wired up they would be fetched (or fail) at validation time.
    """
    assert schema_problems(_minimal_document()) == []


# --- Safe parsing ----------------------------------------------------------


def test_parse_rejects_yaml_alias_bomb():
    """Alias expansion is a resource attack the byte ceiling does not stop."""
    payload = b"a: &anchor [1, 2, 3]\nname: n\nmodel: m\nb: *anchor\n"
    with pytest.raises(AgentSchemaError) as excinfo:
        parse_document(payload)
    assert "alias" in str(excinfo.value).lower()


def test_parse_accepts_markdown_that_looks_like_yaml_syntax():
    """A persona is prose: ``*bold*`` and ``R&D`` are not YAML aliases.

    Guards against the naive "reject any & or * in the source text" approach,
    which would break a large share of real personas.
    """
    payload = (
        b"kind: prompt\nname: n\nmodel: m\n"
        b"instructions: Use *emphasis*, mention R&D, and cite A & B.\n"
    )
    parsed = parse_document(payload)
    assert parsed.data["instructions"] == "Use *emphasis*, mention R&D, and cite A & B."


def test_parse_rejects_non_object_root():
    with pytest.raises(AgentSchemaError, match="object at the top level"):
        parse_document(b"- one\n- two\n")


def test_parse_rejects_empty_and_non_utf8():
    with pytest.raises(AgentSchemaError, match="empty"):
        parse_document(b"   \n")
    with pytest.raises(AgentSchemaError, match="UTF-8"):
        parse_document(b"kind: \xff\xfe prompt")


def test_parse_reports_syntax_error_location():
    with pytest.raises(AgentSchemaError, match=r"line \d+, column \d+"):
        parse_document(b"name: [unclosed\n")


def test_parse_rejects_excessive_nesting():
    depth = spec._MAX_PARSE_DEPTH + 5
    payload = ("a:\n" + "".join(" " * (2 * i) + "b:\n" for i in range(1, depth))).encode()
    with pytest.raises(AgentSchemaError, match="nests deeper"):
        parse_document(payload)


def test_parse_flags_extension_content_mismatch():
    """A ``.yaml`` file holding JSON still parses — but the user is told."""
    parsed = parse_document(b'{"kind": "prompt", "name": "n"}', filename="agent.yaml")
    assert parsed.syntax == "json"
    assert any("contains JSON rather than YAML" in note for note in parsed.warnings)


# --- Normalization leniency ------------------------------------------------


def test_normalize_defaults_missing_kind_to_prompt():
    """``AgentDefinition`` requires ``kind``, yet real documents omit it."""
    document, notes = normalize_document({"name": "n", "model": "gpt-4o"})
    assert document["kind"] == AGENTSCHEMA_SUPPORTED_KIND
    assert any("no 'kind'" in note for note in notes)


def test_normalize_expands_model_shorthand():
    document, notes = normalize_document(_minimal_document(model="gpt-4o"))
    assert document["model"] == {"id": "gpt-4o"}
    assert any("shorthand" in note for note in notes)


def test_normalize_expands_record_shaped_tools():
    """``RecordTool`` (a mapping) is spec-valid and what the examples use."""
    document, notes = normalize_document(
        _minimal_document(
            tools={
                "code_tool": {"kind": "code_interpreter", "fileIds": []},
                "search_tool": {"kind": "file_search", "vectorStoreIds": ["v"]},
            }
        )
    )
    assert isinstance(document["tools"], list)
    assert {tool["name"] for tool in document["tools"]} == {"code_tool", "search_tool"}
    assert any("mapping" in note for note in notes)


def test_normalize_does_not_mutate_the_caller_mapping():
    original = {"name": "n", "model": "gpt-4o"}
    normalize_document(original)
    assert original == {"name": "n", "model": "gpt-4o"}


def test_normalize_rejects_non_object_tool_entry():
    with pytest.raises(AgentSchemaError, match="tools/broken"):
        normalize_document(_minimal_document(tools={"broken": "not-an-object"}))


def test_microsoft_published_example_validates_after_normalization():
    """Microsoft's ``examples/analyst/analyst.yaml`` shape must import.

    It omits ``kind`` and uses the string shorthands for ``model`` and
    ``connection`` — an importer that rejects the spec's own example is not a
    useful importer.
    """
    analyst = yaml.safe_load(
        """
        name: Sales Analyst Agent
        description: Analyses sales data
        metadata:
          tags: [example, analyst]
        model: gpt-4o
        tools:
          code_interpreter_tool:
            kind: code_interpreter
            description: Run code
            fileIds: [sales_data_csv]
          file_search_tool:
            kind: file_search
            connection: file_search_connection
            vectorStoreIds: [sales_data_vector_store]
        instructions: You are an expert sales analyst.
        """
    )
    document, notes = validate_import_document(analyst)
    assert document["kind"] == "prompt"
    assert len(notes) == 3  # missing kind + model shorthand + record tools


# --- Validation ------------------------------------------------------------


@pytest.mark.parametrize("kind", ["hosted", "workflow"])
def test_validate_rejects_unsupported_kinds_with_a_clear_message(kind):
    """``hosted`` / ``workflow`` are refused outright, not partially imported."""
    with pytest.raises(AgentSchemaError) as excinfo:
        validate_import_document(_minimal_document(kind=kind))
    message = str(excinfo.value)
    assert f"kind: {kind}" in message
    assert "only import 'kind: prompt'" in message


def test_validate_rejects_unknown_kind():
    with pytest.raises(AgentSchemaError, match="Unknown AgentSchema kind"):
        validate_import_document(_minimal_document(kind="chatbot"))


def test_validate_reports_field_level_problems():
    with pytest.raises(AgentSchemaError) as excinfo:
        validate_import_document({"kind": "prompt", "instructions": 12})
    problems = excinfo.value.problems
    assert any("'model' is a required property" in problem for problem in problems)
    assert any("'name' is a required property" in problem for problem in problems)
    assert any(problem.startswith("instructions:") for problem in problems)
    # ``PromptAgent`` pulls in ``AgentDefinition`` via ``allOf``, so a missing
    # shared-required property is complained about twice upstream.
    assert len(problems) == len(set(problems))


def test_validate_accepts_a_well_formed_document():
    document, notes = validate_import_document(_minimal_document())
    assert document["name"] == "test-agent"
    assert notes == []


def test_model_options_require_their_discriminator():
    """``ModelOptions`` declares ``kind`` required — easy to forget on export."""
    assert schema_problems(_minimal_document(model={"id": "m", "options": {}})) != []
    assert (
        schema_problems(_minimal_document(model={"id": "m", "options": {"kind": "chat"}}))
        == []
    )


def test_dump_yaml_preserves_key_order_and_unicode():
    text = spec.dump_yaml({"kind": "prompt", "name": "Agent Ré", "model": {"id": "m"}})
    assert text.index("kind:") < text.index("name:") < text.index("model:")
    assert "Ré" in text
