"""The gate decision records ``[service.lab-ctem-screens]`` seeds on its first case (#1628).

``seed_ctem_gates.py`` is loaded by path and its plan checked against the case page's record statuses
(``ctem_case_gate_records``); ``lab.py`` is read from its syntax tree. dev-docs/reference/agentic-labs.md,
"The xtm-one lab".
"""

from __future__ import annotations

import ast
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB_DIR = ROOT / ".xtm-one" / "lab"
LAB = LAB_DIR / "lab.py"
SEED = LAB_DIR / "seed_ctem_gates.py"
DOCS = ROOT / "dev-docs" / "reference" / "agentic-labs.md"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SEED.is_file(), reason="no .xtm-one next to the backend")


def _seed():
    spec = importlib.util.spec_from_file_location("lab_seed_ctem_gates_under_test", SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_seed_covers_every_record_status_on_known_gate_keys():
    from app.agents.builtin_fleet_docs import CTEM_GATE_KEYS
    from app.services import ctem_case_gate_records as records

    module = _seed()
    statuses = {status for _, _, status in module.GATES} | {"passed_automatically"}
    assert statuses == set(records.STATUSES)
    gates = [gate for gate, _, _ in module.GATES] + [module.POLICY_GATE[0]]
    assert len(set(gates)) == len(gates) and set(gates) <= set(CTEM_GATE_KEYS)
    for gate, prefix, _ in module.GATES:
        # The case page finds the item by the same key the personas file it under.
        assert records.item_keys("c")[prefix + "c"] == gate
    reason = records.reason_kind(module.POLICY_GATE[1], "passed_automatically")
    assert reason and reason["kind"] == "runbook"


def test_the_plan_resolves_to_the_records_the_cards_draw():
    from app.services import ctem_case_gate_records as records

    module = _seed()
    now = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
    rows = module.plan("case-1", now)
    history = []
    decisions = []
    for row in rows:
        for outcome, at, by, option in row["events"]:
            history.append(
                {
                    "kind": "gate",
                    "from": row["gate"],
                    "to": outcome,
                    "at": at.isoformat(),
                    "reason": by.replace("{admin}", "admin"),
                    "evidence": option,
                }
            )
        status = {"waiting": "open"}.get(row["record"], row["record"])
        decision = {
            "status": status,
            "created_at": row["asked_at"],
            "deadline": row["deadline"],
            "ask": module.ask(row["gate"], row["deadline"]),
        }
        if row["record"] == "answered":
            decision.update(answer={"option": "A"}, answered_by="admin", answered_at=row["answered_at"])
        decisions.append({"gate": row["gate"], "work_item_id": row["key"], "decision": decision})
    history.append(
        {
            "kind": "gate",
            "from": module.POLICY_GATE[0],
            "to": "passed_automatically",
            "at": now.isoformat(),
            "reason": module.POLICY_GATE[1],
        }
    )
    resolved = {r["key"]: r for r in records.resolve(history, decisions, now=now)}
    assert {k: r["status"] for k, r in resolved.items()} == {
        "ctem.analyst_review": "answered",
        "ctem.validation.non_readonly": "waiting",
        "ctem.remediation.decision": "expired",
        "ctem.mobilize": "cancelled",
        "ctem.accept_risk": "passed_automatically",
    }
    answered = resolved["ctem.analyst_review"]
    assert answered["option"] == {"key": "A", "label": module.OPTIONS[0]["label"]}
    assert answered["wait_seconds"] == 42 * 60
    assert resolved["ctem.validation.non_readonly"]["deadline"]


def test_lab_runs_it_after_the_cases_and_skips_on_failure():
    source = LAB.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_ctem_gates"])
    assert "seed_ctem_gates.py" in helper and "seed_script_env()" in helper and "/ctem-cases/" in helper
    assert source.index("seed_ctem_screens(token, base))") < source.index("seed_ctem_gates(token, base))")
    assert "skipped['CTEM case gates']" in ast.unparse(tree) or 'skipped["CTEM case gates"]' in source
    docs = DOCS.read_text(encoding="utf-8")
    assert "seed_ctem_gates" in docs and "seeded CTEM case gates" in docs
