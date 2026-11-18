"""The background tasks seed of the lab (dev:harness-gaps lab-seed-running-background-tasks)."""

from __future__ import annotations

import ast
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_background_tasks.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")


def _script():
    spec = importlib.util.spec_from_file_location("lab_seed_background_tasks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(name: str) -> str:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    return ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name))


def _plan(now: datetime) -> list[dict]:
    return _script().plan(now, admin=str(uuid.uuid4()), parent=str(uuid.uuid4()), target=str(uuid.uuid4()))


def _expected_counts() -> dict[str, int]:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    node = next(
        n
        for n in tree.body
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "LAB_BACKGROUND_TASKS" for t in n.targets)
    )
    return ast.literal_eval(node.value)


def test_the_plan_matches_the_counts_the_lab_reads_back():
    rows = _plan(datetime.now(UTC))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["task"]["status"]] = counts.get(row["task"]["status"], 0) + 1
    assert counts == _expected_counts()


def test_every_status_is_one_the_model_and_the_listing_know():
    from app.services.sub_agent import background_task_statuses

    statuses = sorted({row["task"]["status"] for row in _plan(datetime.now(UTC))})
    assert background_task_statuses(",".join(statuses)) == statuses


def test_open_tasks_have_a_parked_live_execution_and_ended_ones_none():
    from app.services.orchestration_adapters.sub_agent import ADAPTER_NAME, execution_dedup_key

    now = datetime.now(UTC)
    for row in _plan(now):
        task, execution = row["task"], row["execution"]
        if task["status"] in ("pending", "running"):
            assert execution["adapter"] == ADAPTER_NAME
            assert execution["state"] == "pending"
            assert execution["dedup_key"] == execution_dedup_key(task["id"])
            assert execution["run_at"] >= now + timedelta(days=7)
            assert execution["deadline_at"] is None
            assert task["completed_at"] is None
        else:
            assert execution is None
            assert task["completed_at"] is not None and task["completed_at"] <= now
            assert task["result"] if task["status"] == "completed" else task["error"]


def test_two_running_tasks_have_long_titles():
    running = [r["task"] for r in _plan(datetime.now(UTC)) if r["task"]["status"] == "running"]
    assert sum(1 for t in running if len(t["task_description"]) > 150) >= 2


def test_ids_are_distinct_per_seed():
    first = {r["task"]["id"] for r in _plan(datetime.now(UTC))}
    second = {r["task"]["id"] for r in _plan(datetime.now(UTC) + timedelta(seconds=1))}
    assert len(first) == len(_script().TASKS) and not first & second


def test_the_seed_reads_back_and_logs():
    helper = _source("seed_background_tasks")
    for part in (
        "'seed_background_tasks.py'",
        "'seed'",
        "sys.executable",
        "env=seed_script_env()",
        "/chat/sub-agent-tasks?agent_id=",
        "execution_state",
        "seeded background tasks",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part


def test_every_lab_runs_the_seed():
    seed = _source("seed_entities")
    assert "seed_background_tasks(token, base)" in seed
    assert "skipped['background tasks']" in seed
