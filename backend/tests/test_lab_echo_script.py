"""The lab's ``lab:echo`` script and the seeded Markdown coverage conversation.

A retest that asks the agent to echo a pasted Markdown test (tables of 6 and 8 columns, a 3-level
list with a numbered sub-list and a task list, a blockquote, five fenced languages) needs the stub
model to answer that text verbatim. lab.py starts the stores when imported, so its functions are
compiled one by one from its syntax tree (as ``test_lab_stream_bursts_10910.py`` does).
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_chat_layout.py"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")

NAMES = (
    "parse_lab_script",
    "parse_lab_call",
    "parse_lab_stream",
    "parse_lab_echo",
    "echo_chunks",
    "LAB_SCRIPT_MAX_HOLD_SECONDS",
    "LAB_WAIT_DEFAULT_SECONDS",
    "LAB_WAIT_MAX_SECONDS",
    "LAB_STREAM_DEFAULTS",
    "LAB_STREAM_MAX_BURSTS",
    "LAB_STREAM_MAX_GAP_MS",
    "LAB_ECHO_PER_BURST",
    "LAB_ECHO_GAP_MS",
    "LAB_ECHO_MAX_CHARS",
    "LAB_CHAT_SCRIPTS",
)


def _lab_namespace() -> dict:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (isinstance(node, ast.FunctionDef) and node.name in NAMES)
        or (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in NAMES for t in node.targets))
    ]
    namespace: dict = {"json": json}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


def _coverage() -> str:
    spec = importlib.util.spec_from_file_location("lab_seed_chat_layout_echo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MARKDOWN_COVERAGE


def test_the_echo_script_answers_the_text_after_the_first_line_verbatim():
    parse = _lab_namespace()["parse_lab_script"]
    markdown = _coverage()
    assert parse(f"lab:echo\n{markdown}") == {"script": "echo", "text": markdown}
    assert parse("LAB:ECHO\n  indented\n\nlast line") == {"script": "echo", "text": "  indented\n\nlast line"}


def test_the_echo_script_falls_back_to_the_first_line_then_answers_the_usage():
    parse = _lab_namespace()["parse_lab_script"]
    assert parse("lab:echo **bold** word") == {"script": "echo", "text": "**bold** word"}
    refused = parse("lab:echo")
    assert refused["script"] == "echo" and "nothing to echo" in refused["error"]
    assert "text" not in refused


def test_the_echo_text_is_capped():
    ns = _lab_namespace()
    script = ns["parse_lab_script"]("lab:echo\n" + "x" * (ns["LAB_ECHO_MAX_CHARS"] + 10))
    assert len(script["text"]) == ns["LAB_ECHO_MAX_CHARS"]


def test_the_echo_pieces_are_small_and_rebuild_the_text_exactly():
    ns = _lab_namespace()
    markdown = _coverage()
    pieces = ns["echo_chunks"](markdown)
    assert "".join(pieces) == markdown
    assert all(1 <= len(piece) <= 9 for piece in pieces)
    assert ns["echo_chunks"]("") == []


def test_the_coverage_answer_carries_every_element_a_chat_retest_checks():
    markdown = _coverage()
    lines = markdown.splitlines()
    separators = [line for line in lines if line.startswith("|---")]
    assert sorted(line.count("---") for line in separators) == [6, 8]
    for part in (
        "  - Level two",
        "    - Level three",
        "    1. ",
        "  - [x] ",
        "  - [ ] ",
        "\n> ",
        "```python",
        "```typescript",
        "```json",
        "```bash",
        "```sql",
    ):
        assert part in markdown, part


def test_the_echo_script_is_named_in_the_start_log():
    assert any(name.startswith("lab:echo") for name in _lab_namespace()["LAB_CHAT_SCRIPTS"])


def test_the_stub_streams_the_echo_in_bursts_and_logs_the_coverage_conversation():
    source = LAB.read_text(encoding="utf-8")
    tree = ast.parse(source)
    stub = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "StubModel")
    methods = {node.name: ast.unparse(node) for node in stub.body if isinstance(node, ast.FunctionDef)}
    assert "echo_chunks(script['text'])" in methods["_stream_bursts"]
    assert "script['script'] in ('stream', 'echo')" in methods["do_POST"]
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "seeded chat layout markdown coverage: " in ast.unparse(functions["seed_chat_layout"])
