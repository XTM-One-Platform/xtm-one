"""The lab's ``lab:stream`` script: a long Markdown answer sent in provider-like bursts (#10910).

The chat-layout program measures how evenly the chat reveals streamed text; ``lab:stream`` gives
the stub model a bursty stream to measure against. lab.py starts the stores when imported, so its
functions are compiled one by one from its syntax tree (as ``test_lab_stream_script.py`` does).
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"

NAMES = (
    "parse_lab_script",
    "parse_lab_call",
    "parse_lab_stream",
    "stream_chunks",
    "LAB_SCRIPT_MAX_HOLD_SECONDS",
    "LAB_WAIT_DEFAULT_SECONDS",
    "LAB_WAIT_MAX_SECONDS",
    "LAB_STREAM_DEFAULTS",
    "LAB_STREAM_MAX_BURSTS",
    "LAB_STREAM_MAX_GAP_MS",
    "LAB_STREAM_BODY",
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


def test_the_stream_script_reads_bursts_gap_and_chunks_per_burst():
    parse = _lab_namespace()["parse_lab_script"]
    assert parse("lab:stream") == {"script": "stream", "bursts": 120, "gap_ms": 60, "per_burst": 4}
    assert parse("lab:stream 50 200 12") == {"script": "stream", "bursts": 50, "gap_ms": 200, "per_burst": 12}
    assert parse("lab:stream 10") == {"script": "stream", "bursts": 10, "gap_ms": 60, "per_burst": 4}


def test_the_stream_script_is_capped():
    parse = _lab_namespace()["parse_lab_script"]
    assert parse("lab:stream 999999 999999 999") == {
        "script": "stream",
        "bursts": 2000,
        "gap_ms": 2000,
        "per_burst": 50,
    }
    assert parse("lab:stream 0 0 0") == {"script": "stream", "bursts": 1, "gap_ms": 0, "per_burst": 1}


def test_the_stream_pieces_are_small_and_rebuild_the_markdown():
    ns = _lab_namespace()
    pieces = ns["stream_chunks"]({"bursts": 150, "per_burst": 4, "gap_ms": 60})
    assert len(pieces) == 600
    assert all(3 <= len(piece) <= 9 for piece in pieces)
    text = "".join(pieces)
    # The answer carries what the reveal must render while it streams: a heading, a table, code blocks.
    assert text.startswith(ns["LAB_STREAM_BODY"][:200])
    for marker in ("## Findings", "| Indicator |", "```python", "```json"):
        assert marker in text


def test_the_stream_script_is_named_in_the_start_log():
    assert any(name.startswith("lab:stream") for name in _lab_namespace()["LAB_CHAT_SCRIPTS"])
