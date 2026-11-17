"""The lab seeds a generated documentation on its corporate and sample fleets (#10179).

lab.py is standard library only and starts the stores when imported, so ``seed_fleet_documentation``
is compiled alone from its syntax tree (as ``test_lab_doc_draft.py`` does) with a fake ``api`` and
``log``. dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"


def _seed(structured: dict | None):
    calls: list[tuple[str, str, dict | None]] = []
    logged: list[str] = []

    def api(method: str, path: str, token: str | None = None, body: dict | None = None):
        calls.append((method, path, body))
        return {"structured": structured} if method == "GET" else {}

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "seed_fleet_documentation"]
    assert len(body) == 1
    namespace: dict = {"api": api, "log": logged.append}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace["seed_fleet_documentation"], calls, logged


def test_each_fleet_is_regenerated_whole_read_back_and_logged():
    seed, calls, logged = _seed({"purpose_line": "x"})
    seeded = seed("t", "http://lab", {"corporate fleet": "c1", "sample fleet": "s1"})
    assert calls == [
        ("POST", "/fleets/c1/documentation/regenerate", {}),
        ("GET", "/fleets/c1/documentation", None),
        ("POST", "/fleets/s1/documentation/regenerate", {}),
        ("GET", "/fleets/s1/documentation", None),
    ]
    assert logged == [
        "seeded corporate fleet documentation: http://lab/fleets/c1",
        "seeded corporate fleet documentation: http://lab/fleets/s1",
    ]
    assert seeded["corporate fleet documentation"] == {"id": "c1", "url": "http://lab/fleets/c1"}


def test_markdown_steps_past_the_fold_are_logged():
    """#10327: more than five steps, each with a markdown body, print the markdown steps line."""
    steps = [{"title": f"Step {i}", "detail": f"**Member** acts {i}."} for i in range(7)]
    seed, _, logged = _seed({"steps": steps})
    seed("t", "http://lab", {"corporate fleet": "c1"})
    assert logged == [
        "seeded corporate fleet documentation: http://lab/fleets/c1",
        "seeded fleet doc markdown steps: http://lab/fleets/c1",
    ]


def test_a_documentation_without_structured_parts_is_refused():
    seed, _, logged = _seed(None)
    with pytest.raises(RuntimeError, match="no structured parts"):
        seed("t", "http://lab", {"corporate fleet": "c1"})
    assert logged == []
