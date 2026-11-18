"""The xtm-one lab seeds crossed Concierge signals and cycles (dev:lab-seed-concierge-signals).

The planned rows must cover every signal kind, every target type, a cycle started, the known and an
unknown refusal, a ``fired_at`` null and a ``last_value`` null, so the Signals tab renders every state
on real rows with no ``window.fetch`` override.
"""

from __future__ import annotations

import ast
import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_concierge_signals.py"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_seed_concierge_signals", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows():
    targets = {kind: str(uuid.uuid4()) for kind in ("agent", "flow", "assignment")}
    return targets, _module().plan_signals(targets, datetime.now(UTC))


def test_the_signals_cover_every_kind_target_and_state():
    from app.services.health.signals import SIGNAL_KINDS

    targets, rows = _rows()
    assert len(rows) >= 6
    assert {row["kind"] for row in rows} == set(SIGNAL_KINDS)
    assert {row["target_type"] for row in rows} == {"agent", "flow", "assignment"}
    assert all(row["target_id"] == targets[row["target_type"]] and row["active"] for row in rows)
    assert any(row["cycle"] for row in rows)
    assert any(row["fired_at"] is None for row in rows)
    assert any(row["last_value"] is None for row in rows)
    # One row per (target, kind): the table's unique constraint.
    assert len({(row["target_type"], row["kind"]) for row in rows}) == len(rows)


def test_the_refusals_are_known_codes_and_one_unknown():
    from app.services.health.cycles import CycleRefusal

    module = _module()
    _, rows = _rows()
    refusals = {row["refusal"] for row in rows if row["refusal"]}
    assert refusals == set(module.REFUSALS)
    known = set(get_args(CycleRefusal))
    assert {"cycle_open", "month_budget_reached"} <= known
    assert module.UNKNOWN_REFUSAL not in known
    # A started cycle carries no refusal.
    assert all(not (row["cycle"] and row["refusal"]) for row in rows)


def test_the_lab_starts_the_seed_and_reads_the_signals_back():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_concierge_signals"])
    for part in (
        "'seed_concierge_signals.py'",
        "sys.executable",
        "env=seed_script_env()",
        "/concierge/signals?active_only=true",
        "/concierge/signals",
        "concierge signals (",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_concierge_signals(token, ids['agent'], base)"
    assert call in seeded and "skipped['concierge signals']" in seeded
    assert seeded.index(call) < seeded.index("seeded.json")
