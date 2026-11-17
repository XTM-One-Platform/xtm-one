"""The lab's evaluation run seed: what the two runs cover and how ``lab.py`` runs the script.

The runs are built by a plain function of ``.xtm-one/lab/seed_evaluation_runs.py`` (no database);
the script is loaded by path. dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import importlib.util
import uuid
from pathlib import Path

import pytest

from app.models.evaluation import EvaluationRun

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = ROOT / ".xtm-one" / "lab" / "seed_evaluation_runs.py"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")

#: Keys of a run spec that are not ``EvaluationRun`` columns: the seed's own clock and result.
SPEC_ONLY = {"age_minutes", "minutes_to_finish", "result_score"}


def _runs():
    spec = importlib.util.spec_from_file_location("lab_seed_evaluation_runs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.runs(str(uuid.uuid4()))


def test_one_completed_run_with_headlines_and_one_running_run_without_scores():
    completed, running = _runs()
    assert completed["status"] == "completed"
    assert completed["quality_score"] is not None and completed["performance_score"] is not None
    assert completed["minutes_to_finish"] is not None and completed["result_score"] is not None
    assert running["status"] == "running"
    assert running["score"] is None and running["quality_score"] is None and running["performance_score"] is None
    assert running["minutes_to_finish"] is None and running["result_score"] is None
    assert 0 < running["completed_entries"] < running["total_entries"]


def test_the_running_run_names_an_execution_so_the_stranded_sweep_never_takes_it():
    # An unowned running run is failed by sweep_stranded_runs once no live execution names its
    # evaluation; an owned one only through its execution's terminal clock, absent here.
    completed, running = _runs()
    assert isinstance(running.get("owner_execution_id"), uuid.UUID)
    assert "owner_execution_id" not in completed


def test_every_spec_key_is_a_run_column():
    columns = set(EvaluationRun.__table__.columns.keys())
    for spec in _runs():
        assert set(spec) - SPEC_ONLY <= columns, set(spec) - SPEC_ONLY - columns


def test_the_copy_carries_no_dash_of_the_banned_kinds():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "\u2014" not in source and "\u2013" not in source


def test_the_lab_runs_the_script_after_the_evaluation_and_logs_each_run_url():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_evaluation_runs"])
    for part in (
        "'seed_evaluation_runs.py'",
        "sys.executable",
        "env=seed_script_env()",
        "stderr_summary(process.stderr)",
        "/evaluations/runs/{run}",
        "'/evaluations/runs/search'",
        "seeded evaluation runs: {url}",
    ):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert (
        seed.index("for entity, route, create in SEEDS")
        < seed.index("seed_evaluation_runs(token, ids['evaluation'], ids['agent'], ids['dataset'], base)")
        < seed.index("seeded.json")
    )
