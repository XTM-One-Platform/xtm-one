"""The lab seeds an agent's memory rows and a fleet gate review (#11001).

lab.py is standard library only and starts the stores when imported, so the two seeds are compiled
alone from its syntax tree (as ``test_lab_fleet_documentation_seed.py`` does) with a fake ``api``.
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_fleet_gate_review.py"
NAMES = {
    "AGENT_MEMORY_SUBJECT",
    "AGENT_MEMORY_LONG",
    "AGENT_MEMORY_ENTRIES",
    "agent_memory_entries",
    "seed_agent_memory",
    "seed_fleet_gate_review",
    "_created",
}


def _compile(api, run=None) -> dict:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    body = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.FunctionDef):
            targets = [node.name]
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        if set(targets) & NAMES:
            body.append(node)
    namespace: dict = {
        "api": api,
        "log": lambda _line: None,
        "time": time,
        "SEED_NAME": "Lab sample",
        "LAB_DOC_GENERATING_SETTLE_SECONDS": 1,
        "_seed_agent": lambda _token, name, handovers=None: f"agent-{name.split()[0]}",
        "subprocess": SimpleNamespace(run=run or (lambda *a, **k: SimpleNamespace(returncode=0, stderr=""))),
        "sys": SimpleNamespace(executable="python"),
        "CHECKOUT": ROOT,
        "seed_script_env": dict,
        "stderr_summary": lambda text: text,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


def test_the_memory_entries_cover_every_state_the_memory_tab_shows():
    ns = _compile(api=None)
    entries = ns["agent_memory_entries"]("admin-1", 2_000_000_000.0)
    assert 25 < len(entries) <= 200
    assert {e["memory_type"] for e in entries} == {"fact", "learned", "preference", "style", "state", "consolidated"}
    assert {e["source"] for e in entries} == {"chat", "assignment"}
    chain = [e for e in entries if e.get("subject_key") == ns["AGENT_MEMORY_SUBJECT"]]
    assert len(chain) >= 3 and {e["memory_type"] for e in chain} == {"state"}
    assert any(len(e["content"]) > 250 for e in entries)
    assert any(e.get("user_id") == "admin-1" for e in entries)
    assert any("user_id" not in e for e in entries)
    assert all(e["created_at"].endswith("Z") for e in entries)
    assert len({e["created_at"] for e in entries}) == len(entries)


def test_the_memory_seed_imports_reads_back_and_answers_the_memory_url():
    calls: list[tuple[str, str]] = []

    def api(method, path, token=None, body=None):
        calls.append((method, path))
        if path == "/auth/me":
            return {"id": "admin-1"}
        if method == "POST":
            return {"imported": len(body["entries"])}
        return {"total_count": len(ns["AGENT_MEMORY_ENTRIES"])}

    ns = _compile(api)
    seeded = ns["seed_agent_memory"]("t", "http://lab")
    assert ("POST", "/agents/agent-memory/memory") in calls
    assert ("GET", "/agents/agent-memory/memory?limit=1") in calls
    assert seeded == {"agent memory": {"id": "agent-memory", "url": "http://lab/agents/agent-memory/memory"}}


def test_the_memory_seed_refuses_a_short_import():
    def api(method, path, token=None, body=None):
        return {"id": "admin-1"} if path == "/auth/me" else {"imported": 1}

    ns = _compile(api)
    with pytest.raises(RuntimeError, match="did not store every entry"):
        ns["seed_agent_memory"]("t", "http://lab")


def _fleet_api(gates: dict):
    calls: list[tuple[str, str, dict | None]] = []

    def api(method, path, token=None, body=None):
        calls.append((method, path, body))
        if path == "/fleets" and method == "GET":
            return []
        if path == "/fleets" and method == "POST":
            return {"id": "f1"}
        if path.endswith("/members"):
            return {"id": "m1"}
        if path.endswith("/documentation"):
            return {"version": 1}
        if path.endswith("/approval-gates") and method == "GET":
            return gates
        return {}

    return api, calls


def test_the_gate_review_seed_clears_the_catalog_and_answers_the_approval_url():
    api, calls = _fleet_api({"effective_catalog": [], "computed_catalog_on": False, "proposals": [{"key": "k"}]})
    ran: list[list[str]] = []
    ns = _compile(api, run=lambda argv, **_: ran.append(argv) or SimpleNamespace(returncode=0, stderr=""))
    seeded = ns["seed_fleet_gate_review"]("t", "http://lab", "gh-1")
    assert ("PUT", "/agents/agent-gate", {"integration_bindings": {"github": ["gh-1"]}}) in calls
    assert ("PUT", "/fleets/f1/approval-gates", {"gates": []}) in calls
    assert ran and ran[0][1].endswith("seed_fleet_gate_review.py") and ran[0][2] == "f1"
    assert seeded == {"fleet gate review": {"id": "f1", "url": "http://lab/fleets/f1/approval"}}


@pytest.mark.parametrize(
    "gates",
    [
        {"effective_catalog": [{"key": "k"}], "computed_catalog_on": False, "proposals": [{"key": "k"}]},
        {"effective_catalog": [], "computed_catalog_on": True, "proposals": [{"key": "k"}]},
        {"effective_catalog": [], "computed_catalog_on": False, "proposals": []},
    ],
)
def test_the_gate_review_seed_refuses_a_fleet_that_would_not_offer_the_review(gates):
    api, _ = _fleet_api(gates)
    ns = _compile(api)
    with pytest.raises(RuntimeError, match="catalog or nothing to review"):
        ns["seed_fleet_gate_review"]("t", "http://lab", "gh-1")


def test_both_seeds_run_from_seed_entities_before_the_seeded_file():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    seed = ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "seed_entities"))
    for call, key in (
        ("seed_agent_memory(token, base)", "skipped['agent memory']"),
        ("seed_fleet_gate_review(token, base, seeded['GitHub integration']['id'])", "skipped['fleet gate review']"),
    ):
        assert call in seed and key in seed
        assert seed.index(call) < seed.index("seeded.json")


def test_the_helper_script_clears_the_pins_of_one_custom_fleet_only():
    script = ast.unparse(ast.parse(SCRIPT.read_text(encoding="utf-8")))
    assert "fleet.approval_pins = None" in script
    assert "fleet.is_builtin" in script
    assert "await db.commit()" in script
