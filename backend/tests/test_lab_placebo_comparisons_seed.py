"""The lab's placebo comparison seed: what the three comparisons cover and how ``lab.py`` runs the script.

The comparisons are built by plain functions of ``.xtm-one/lab/seed_placebo_comparisons.py`` (no
database); the script is loaded by path. dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import importlib.util
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = ROOT / ".xtm-one" / "lab" / "seed_placebo_comparisons.py"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _script():
    spec = importlib.util.spec_from_file_location("lab_seed_placebo_comparisons", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _comparisons():
    script = _script()
    skills = (str(uuid.uuid4()), str(uuid.uuid4()))
    return script, skills, script.comparisons(str(uuid.uuid4()), skills)


def test_the_first_comparison_covers_every_state_a_row_renders():
    script, skills, (first, _, _) = _comparisons()
    assert first["status"] == "completed"
    rows = first["rows"]
    assert len(rows) == 7
    assert {row["verdict"] for row in rows} == set(script.VERDICTS)
    assert {row["target"]["kind"] for row in rows} == {"persona", "prompts", "skills", "skill"}
    assert {row["target"]["skill_id"] for row in rows if row["target"]["kind"] == "skill"} == set(skills)
    # A measured skill target carries the Skill opened share on both arms.
    for row in rows:
        if row["target"]["kind"] in {"skill", "skills"}:
            assert row["arms"]["real"]["skill_loaded"] is not None
            assert row["arms"]["placebo"]["skill_loaded"] is not None
    assert any(row["rests_on_tokens_alone"] and row["vs_placebo"]["quality"] for row in rows)
    assert {1, 2, 4} <= {len(row["warnings"]) for row in rows}
    assert any("few_entries" in row["warnings"] for row in rows)
    assert any(len(row["agent_name"]) >= 60 for row in rows)
    assert any(len(row["model_id"]) >= 60 for row in rows)
    # One row per agent x model x target, as the card keys them.
    keys = [(row["agent_id"], row["model_id"], row["target_key"]) for row in rows]
    assert len(keys) == len(set(keys))


def test_the_second_has_no_skill_target_and_the_third_failed_without_results():
    _, _, (_, second, third) = _comparisons()
    assert second["status"] == "completed" and second["rows"]
    assert all(row["arms"]["real"]["skill_loaded"] is None for row in second["rows"])
    assert all(row["target"]["kind"] not in {"skill", "skills"} for row in second["rows"])
    assert third["status"] == "failed" and third["rows"] is None


def test_the_comparisons_are_newest_first_and_settled():
    _, _, comparisons = _comparisons()
    ages = [comparison["age_hours"] for comparison in comparisons]
    assert ages == sorted(ages)
    assert {comparison["status"] for comparison in comparisons} <= {"completed", "failed", "cancelled"}


def test_the_warning_codes_are_the_frontends_known_codes():
    effects = ROOT / "frontend" / "src" / "components" / "evaluation" / "placebo-warning-effects.ts"
    if not effects.is_file():
        pytest.skip("no frontend next to the backend")
    text = effects.read_text(encoding="utf-8")
    _, _, comparisons = _comparisons()
    codes = {code for comparison in comparisons for row in comparison["rows"] or () for code in row["warnings"]}
    codes |= {warning["code"] for comparison in comparisons for warning in comparison["warnings"]}
    assert all(f"  {code}:" in text for code in codes), codes


def test_the_copy_carries_no_dash_of_the_banned_kinds():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "\u2014" not in source and "\u2013" not in source


def test_the_lab_runs_the_script_after_the_evaluation_and_logs_its_url():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_placebo_comparisons"])
    for part in (
        "'seed_placebo_comparisons.py'",
        "sys.executable",
        "env=seed_script_env()",
        "stderr_summary(process.stderr)",
        "'/skills'",
        "/placebo-comparisons?limit=10",
        "seeded placebo comparisons ({len(written)}): {url}",
    ):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert (
        seed.index("for entity, route, create in SEEDS")
        < seed.index("seed_placebo_comparisons(token, ids['evaluation'], ids['agent'], base)")
        < seed.index("seeded.json")
    )
