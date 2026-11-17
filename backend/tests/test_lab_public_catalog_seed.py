"""The lab turns the AI catalog on and publishes one row of each kind, so the /catalog pages render.

lab.py is standard library only and starts the stores when imported, so ``seed_public_catalog``,
``lab_ai_catalog_env`` and ``LAB_PUBLIC_CATALOG`` are compiled alone from its syntax tree (as
``test_lab_shared_resources_seed.py`` does) with a fake ``api`` and ``log``.
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
NAMES = ("seed_public_catalog", "lab_ai_catalog_env", "LAB_PUBLIC_CATALOG")


def _created(answer):
    if not isinstance(answer, dict) or not answer.get("id"):
        raise RuntimeError("no id")
    return answer["id"]


def _load(listed_tags: list[str] | None = None, list_it: bool = True):
    calls: list[tuple[str, str, dict | None]] = []
    logged: list[str] = []
    created: dict[str, str] = {}

    def api(method: str, path: str, token: str | None = None, body: dict | None = None):
        calls.append((method, path, body))
        if method == "POST":
            created[path] = f"id{len(created)}"
            return {"id": created[path]}
        if method == "GET":
            tags = listed_tags if listed_tags is not None else ["lab"]
            items = [{"id": row, "tags": tags} for row in created.values()] if list_it else []
            return {"items": items, "total": len(items)}
        return {}

    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    wanted = [
        n
        for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in NAMES)
        or (isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in NAMES)
    ]
    assert len(wanted) == 3
    namespace: dict = {"api": api, "log": logged.append, "_created": _created, "SEED_NAME": "Lab sample", "os": os}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace, calls, logged


def test_each_kind_is_created_with_tags_published_read_back_and_logged():
    ns, calls, logged = _load()
    seeded = ns["seed_public_catalog"]("t", "http://lab")
    posts = [(path, body) for method, path, body in calls if method == "POST"]
    assert [path for path, _ in posts] == ["/agents", "/skills", "/prompts", "/mcp-servers", "/custom-tools"]
    assert all(body["tags"] for _, body in posts)
    puts = [(path, body) for method, path, body in calls if method == "PUT"]
    assert puts == [
        ("/agents/id0", {"is_public_catalog": True}),
        ("/skills/id1", {"is_public_catalog": True}),
        ("/prompts/id2", {"is_public_catalog": True}),
        ("/mcp-servers/id3", {"is_public_catalog": True}),
        ("/custom-tools/id4", {"is_public_catalog": True}),
    ]
    gets = [path.split("?")[0] for method, path, _ in calls if method == "GET"]
    assert gets == [f"/public/catalog/{s}" for s in ("agents", "skills", "prompts", "mcp-servers", "custom-tools")]
    assert logged == [
        "seeded public catalog: http://lab/catalog/agents http://lab/catalog/skills http://lab/catalog/prompts "
        "http://lab/catalog/mcp-servers http://lab/catalog/tools"
    ]
    assert seeded["catalog custom tool"] == {"id": "id4", "url": "http://lab/catalog/tools/id4"}


def test_a_row_the_catalog_does_not_list_is_refused():
    ns, _, logged = _load(list_it=False)
    with pytest.raises(RuntimeError, match="does not list it"):
        ns["seed_public_catalog"]("t", "http://lab")
    assert logged == []


def test_a_row_listed_without_tags_is_refused():
    ns, _, _ = _load(listed_tags=[])
    with pytest.raises(RuntimeError, match="with no tags"):
        ns["seed_public_catalog"]("t", "http://lab")


def test_the_catalog_is_on_by_default_and_a_declaration_can_turn_it_off(monkeypatch):
    ns, _, _ = _load()
    monkeypatch.delenv("AI_CATALOG_ENABLED", raising=False)
    assert ns["lab_ai_catalog_env"]() == "true"
    monkeypatch.setenv("AI_CATALOG_ENABLED", "False")
    assert ns["lab_ai_catalog_env"]() == "false"
