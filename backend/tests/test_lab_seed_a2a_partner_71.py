"""The A2A partner integration seed of the lab (#71)."""

from __future__ import annotations

import ast
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_a2a_partner_runs.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")


def _script():
    spec = importlib.util.spec_from_file_location("lab_seed_a2a_partner_runs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(name: str) -> str:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    return ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name))


def test_the_plan_has_mixed_statuses_with_one_running():
    rows = _script().plan("a2a-lab", datetime.now(UTC))
    assert [r["status"] for r in rows] == ["completed", "failed", "running"]
    running = rows[-1]
    assert running["completed_at"] is None and running["error"] is None


def test_every_run_carries_the_shape_create_task_writes():
    from app.a2a.providers import OAUTH_A2A_PROVIDER

    for row in _script().plan("a2a-lab", datetime.now(UTC)):
        context = row["trigger_context"]
        assert context["source"] == "a2a"
        assert context["client_id"] == "a2a-lab"
        assert context["a2a_provider"] == OAUTH_A2A_PROVIDER
        assert row["trigger_payload"]["message"]


def test_the_seed_mints_binds_reads_back_and_logs():
    helper = _source("seed_a2a_partner")
    for part in (
        "'provider': 'oauth_a2a'",
        "'a2a_direction': 'inbound'",
        "/a2a-credential",
        "'integration_bindings': {'oauth_a2a': [integration]}",
        "'seed_a2a_partner_runs.py'",
        "env=seed_script_env()",
        "a2a_integration_id=",
        "seeded A2A integration with partner runs",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part


def test_every_lab_runs_the_seed():
    seed = _source("seed_entities")
    assert "seed_a2a_partner(token, base)" in seed
    assert "skipped['A2A partner integration']" in seed
