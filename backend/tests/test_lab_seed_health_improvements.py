"""The xtm-one lab seeds Platform Health improvement proposals and a builder-safe flow (#10401).

The planned rows must cover every proposal type on its own target type, every status, every
experiment verdict and a person's request, keep one open change per target (the table's partial
unique index), and the lab flow must carry the viewport and positions the builder reads.
"""

from __future__ import annotations

import ast
import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_concierge_improvements.py"
SIGNALS = LAB.parent / "seed_concierge_signals.py"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows():
    module = _load(SCRIPT, "lab_seed_concierge_improvements")
    keys = ("agent", "skill1", "skill2", "assignment", "assignment2", "flow", "flow2")
    targets = {key: str(uuid.uuid4()) for key in keys}
    texts = {"agent": "Persona.\n", "skill1": "Skill.\n", "skill2": "Other.\n", "assignment": "Prompt.\n",
             "assignment2": "Sweep.\n"}
    admin, agent = str(uuid.uuid4()), targets["agent"]
    return module, targets, texts, admin, module.plan_proposals(targets, texts, admin, agent, datetime.now(UTC))


def test_the_proposals_cover_every_type_status_and_verdict():
    from app.models.improvement_proposal import EXPERIMENT_VERDICTS, PROPOSAL_STATUSES, PROPOSAL_TYPES, TARGET_OF

    _, _, _, _, rows = _rows()
    assert 25 <= len(rows) <= 40
    assert {row["proposal_type"] for row in rows} == set(PROPOSAL_TYPES)
    assert {row["status"] for row in rows} == set(PROPOSAL_STATUSES)
    assert {row["experiment_verdict"] for row in rows} == {*EXPERIMENT_VERDICTS, None}
    assert all(TARGET_OF[row["proposal_type"]] == row["target_type"] for row in rows)
    assert any(row["summary"] is None for row in rows)
    assert sum(1 for row in rows if row["summary"] and len(row["summary"]) > 200) >= 2
    assert len({row["dedup_key"] for row in rows}) == len(rows)
    assert all(len(row["dedup_key"]) == 64 for row in rows)


def test_one_open_change_per_target_and_pending_rows_start_from_the_live_text():
    from app.models.improvement_proposal import OPEN_STATUSES

    _, targets, texts, admin, rows = _rows()
    open_rows = [row for row in rows if row["status"] in OPEN_STATUSES and row["proposal_type"] != "new_flow"]
    assert len({(row["target_type"], row["target_id"]) for row in open_rows}) == len(open_rows)
    pending = [row for row in rows if row["status"] == "pending_approval"]
    assert len(pending) >= 4
    key_of = {value: key for key, value in targets.items()}
    for row in pending:
        assert "decided_at" not in row
        if row["proposal_type"] in ("persona", "skill", "assignment_prompt"):
            assert row["base_content"] == texts[key_of[row["target_id"]]]
            assert row["proposed_content"] != row["base_content"]
    # The seeded Platform Health flow carries rows of its own, with a valid graph change.
    flow_rows = [row for row in rows if row["target_id"] == targets["flow"]]
    assert flow_rows and all(row["change"]["graph"]["viewport"] for row in flow_rows)
    assert all(row["requested_by_user_id"] == admin for row in rows if row["experiment_verdict"] is None)


def test_the_lab_flow_graph_is_one_the_builder_reads():
    from app.services.flow_graph import normalize_graph, validate_graph

    graph = _load(SIGNALS, "lab_seed_concierge_signals_graph").flow_graph()
    assert graph["viewport"] == {"x": 0, "y": 0, "zoom": 1}
    assert all("position" in node for node in graph["nodes"])
    assert normalize_graph(graph)["viewport"] == graph["viewport"]
    assert validate_graph(graph).is_valid


def test_the_lab_starts_the_seed_and_reads_the_total_back():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_concierge_improvements"])
    for part in (
        "'seed_concierge_improvements.py'",
        "sys.executable",
        "env=seed_script_env()",
        "/concierge/improvements?limit=200",
        "concierge improvements (",
        "/concierge?tab=improvements",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    seeded = ast.unparse(functions["seed_entities"])
    signals = "seed_concierge_signals(token, ids['agent'], base)"
    assert "seed_concierge_improvements(" in seeded and "skipped['concierge improvements']" in seeded
    assert seeded.index(signals) < seeded.index("seed_concierge_improvements(") < seeded.index("seeded.json")
