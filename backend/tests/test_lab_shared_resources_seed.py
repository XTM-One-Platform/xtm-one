"""The lab seeds a shared variable, MCP server and custom tool, so the ``sm`` sharing badge renders (#10501).

lab.py is standard library only and starts the stores when imported, so ``seed_shared_resources`` and
``LAB_SHARED_RESOURCES`` are compiled alone from its syntax tree (as ``test_lab_fleet_documentation_seed.py``
does) with a fake ``api`` and ``log``. dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"


def _created(answer):
    if not isinstance(answer, dict) or not answer.get("id"):
        raise RuntimeError("no id")
    return answer["id"]


def _seed(ways: int):
    calls: list[tuple[str, str, dict | None]] = []
    logged: list[str] = []
    created: dict[str, str] = {}

    def api(method: str, path: str, token: str | None = None, body: dict | None = None):
        calls.append((method, path, body))
        if method == "POST":
            created[path] = f"id{len(created)}"
            return {"id": created[path]}
        if method == "GET":
            return {"summaries": {row: {"ways": ways} for row in created.values()}, "truncated": False}
        return {}

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    wanted = [
        n
        for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name == "seed_shared_resources")
        or (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "LAB_SHARED_RESOURCES")
    ]
    assert len(wanted) == 2
    namespace: dict = {"api": api, "log": logged.append, "_created": _created, "SEED_NAME": "Lab sample"}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace["seed_shared_resources"], calls, logged


def test_features_are_enabled_then_each_row_is_shared_read_back_and_logged():
    seed, calls, logged = _seed(ways=1)
    seeded = seed("t", "http://lab", "peer1")
    assert calls[0] == (
        "PUT",
        "/settings/security-options",
        {"values": {"disable_custom_variables": False, "disable_custom_tools": False}},
    )
    posts = [(path, body) for method, path, body in calls if method == "POST"]
    assert [path for path, _ in posts] == ["/variables", "/mcp-servers", "/custom-tools"]
    for _, body in posts:
        assert body["shared_user_ids"] == ["peer1"]
        assert body["shared_user_permissions"] == {"peer1": "read_only"}
    gets = [path for method, path, _ in calls if method == "GET"]
    assert gets == ["/exposure/variables/summary", "/exposure/mcp-servers/summary", "/exposure/custom-tools/summary"]
    assert logged == ["seeded shared resources: http://lab/variables http://lab/mcp-servers http://lab/tools/custom"]
    assert seeded["shared MCP server"] == {"id": "id1", "url": "http://lab/mcp-servers"}


def test_a_row_shared_no_way_is_refused():
    seed, _, logged = _seed(ways=0)
    with pytest.raises(RuntimeError, match="answers ways 0"):
        seed("t", "http://lab", "peer1")
    assert logged == []
