"""The code_interpreter tool description must state its statefulness contract.

Agents reason about whether a tool persists state between calls. The
code_interpreter runs a fresh, isolated subprocess on every call, so variables
and imports do NOT survive to the next call. That fact was documented only in
the source docstring, so models inferred it mid-task ("state is not persisted
between iterations") and wasted iterations. The tool description the model
actually sees must spell the contract out.

When a NameError occurs (the most common symptom of cross-call state confusion),
format_text_result() must append an actionable per-call isolation reminder so
the model corrects its approach in the very next iteration instead of wasting
more iterations on the same pattern.
"""

from app.services.builtin_tools.media import build_code_interpreter_tool
from app.services.code_interpreter import format_text_result


def test_code_interpreter_description_documents_per_call_statelessness():
    definitions, _executor = build_code_interpreter_tool()
    assert len(definitions) == 1
    definition = definitions[0]
    assert definition.name == "code_interpreter"

    description = definition.description.lower()
    # The contract: each call is isolated and state does not carry over.
    assert "not persisted between calls" in description
    assert "fresh" in description and "isolated" in description
    # Actionable guidance for the model.
    assert "single call" in description
    assert "print()" in definition.description


def test_code_interpreter_code_param_repeats_clean_interpreter_contract():
    definitions, _executor = build_code_interpreter_tool()
    code_desc = definitions[0].input_schema["properties"]["code"]["description"].lower()
    assert "clean interpreter" in code_desc
    assert "no state carried over" in code_desc


# ---------------------------------------------------------------------------
# NameError isolation hint
# ---------------------------------------------------------------------------


def test_format_text_result_appends_isolation_hint_on_nameerror():
    """A NameError result must include the per-call isolation reminder."""
    result = {
        "stdout": "",
        "stderr": "",
        "error": "NameError: name 'agents' is not defined",
        "images": [],
        "output_files": [],
    }
    text = format_text_result(result)
    assert "NameError" in text
    # The hint must be present so the model understands why and fixes immediately.
    text_lower = text.lower()
    assert "fresh, isolated process" in text_lower
    assert "within this single call" in text_lower
    # The hint must also cover the plain-typo case gracefully.
    assert "misspelled" in text_lower


def test_format_text_result_hint_on_realistic_traceback():
    """The real ``error`` field is a full traceback.format_exc() string."""
    result = {
        "stdout": "partial output",
        "stderr": "",
        "error": (
            "Traceback (most recent call last):\n"
            '  File "<code>", line 2, in <module>\n'
            "NameError: name 're' is not defined. Did you forget to import 're'?\n"
        ),
        "images": [],
        "output_files": [],
    }
    text = format_text_result(result)
    assert "fresh, isolated process" in text.lower()
    # The hint is appended exactly once.
    assert text.lower().count("fresh, isolated process") == 1


def test_format_text_result_hint_is_ascii():
    """The hint is model-facing text and must stay plain ASCII."""
    result = {
        "stdout": "",
        "stderr": "",
        "error": "NameError: name 'x' is not defined",
        "images": [],
        "output_files": [],
    }
    text = format_text_result(result)
    hint = text.split("\n\n")[1]
    assert hint.startswith("Hint:")
    assert hint.isascii()


def test_format_text_result_no_hint_for_other_errors():
    """Non-NameError errors must NOT receive the isolation hint."""
    result = {
        "stdout": "",
        "stderr": "",
        "error": "ZeroDivisionError: division by zero",
        "images": [],
        "output_files": [],
    }
    text = format_text_result(result)
    assert "ZeroDivisionError" in text
    assert "isolated process" not in text.lower()


def test_format_text_result_nameerror_without_is_not_defined_no_hint():
    """A NameError-shaped message lacking 'is not defined' must not get the hint."""
    result = {
        "stdout": "",
        "stderr": "",
        "error": "NameError: some other name error message",
        "images": [],
        "output_files": [],
    }
    text = format_text_result(result)
    assert "isolated process" not in text.lower()


def test_format_text_result_markers_on_different_lines_no_hint():
    """'NameError' and 'is not defined' on unrelated traceback lines must not match."""
    result = {
        "stdout": "",
        "stderr": "",
        "error": (
            "Traceback (most recent call last):\n"
            "NameError: name 'cfg' has no usable value\n"
            "\nDuring handling of the above exception, another exception occurred:\n"
            "ValueError: template variable is not defined in the mapping\n"
        ),
        "images": [],
        "output_files": [],
    }
    text = format_text_result(result)
    assert "isolated process" not in text.lower()


def test_format_text_result_no_hint_on_success():
    """Successful executions must not carry the isolation hint."""
    result = {
        "stdout": "42",
        "stderr": "",
        "error": None,
        "images": [],
        "output_files": [],
    }
    text = format_text_result(result)
    assert "isolated process" not in text.lower()
    assert "Hint:" not in text
