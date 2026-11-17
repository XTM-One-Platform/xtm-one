"""#10251: the xtm-one lab seeds knowledge bases with stale stored embeddings.

The plan is checked against the platform target the way ``_load_kb_stale_embedding_info`` reads it
(model or dimensions differ), and the lab wiring (script, read-back, log line, switch, sweeper off)
is read from ``lab.py``, so a change that would leave the stale notice empty in a lab fails here.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_kb_stale_embeddings.py"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_seed_kb_stale_embeddings", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("target", [("text-embedding-3-large", 1024), ("lab-legacy-embedding-v1", 384),
                                    ("lab-legacy-embedding-v2", 768)])
def test_every_planned_row_is_stale_against_any_target(target):
    module = _module()
    rows = module.plan(target)
    assert len(rows) == module.COUNT >= 12
    assert all(module.is_stale(row, target) for row in rows)
    for row in rows:
        assert all((model, dims) != target for model, dims, _ in row["tuples"])


def test_the_plan_covers_the_debug_table_states():
    module = _module()
    rows = module.plan(module.LAB_TARGET)
    names = [row["name"] for row in rows]
    assert len(set(names)) == len(names)
    assert any(len(name) > 100 for name in names)
    assert all(len(name) <= 200 for name in names)  # KnowledgeBase.name is String(200)
    assert [row["package"] for row in rows].count(True) == 1
    assert any(len(row["tuples"]) == 2 for row in rows)
    assert all(n > 0 for row in rows for _, _, n in row["tuples"])


def test_unit_vector_is_never_zero_norm():
    module = _module()
    for dims, axis in ((384, 0), (768, 5), (4, 9)):
        vector = module.unit_vector(dims, axis)
        assert len(vector) == dims and sum(vector) == 1.0


def test_the_lab_starts_the_seed_reads_it_back_and_keeps_it_stale():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_kb_stale_embeddings"])
    for part in (
        "'seed_kb_stale_embeddings.py'",
        "sys.executable",
        "env=seed_script_env()",
        "'/knowledge-bases'",
        "'stale_chunk_count'",
        "'is_package_origin'",
        "'stale embeddings'",
        "/knowledge",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    assert "'LAB_KB_STALE'" in ast.unparse(functions["seeds_kb_stale_embeddings"])
    assert "'EMBEDDING_STALE_AUTO_REMEDIATION_ENABLED'" in ast.unparse(functions["backend_env"])
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_kb_stale_embeddings(token, base)"
    assert call in seeded and "skipped['stale embeddings']" in seeded
    assert seeded.index(call) < seeded.index("seeded.json")


def test_the_switch_gates_the_seed_and_the_sweeper():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    switch = ast.unparse(functions["seeds_kb_stale_embeddings"])
    assert "os.environ.get('LAB_KB_STALE', '1')" in switch and "!= '0'" in switch
    assert "if seeds_kb_stale_embeddings() else {}" in ast.unparse(functions["backend_env"])
    assert "if seeds_kb_stale_embeddings():" in ast.unparse(functions["seed_entities"])
