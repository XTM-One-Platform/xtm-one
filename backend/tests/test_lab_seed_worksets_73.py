"""The implicit (Rolling, with an eviction) and sealed worksets of the lab seed (#73)."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_operations.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")


def _script():
    spec = importlib.util.spec_from_file_location("lab_seed_operations", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_rolling_items_overflow_and_evict_the_lowest_one():
    module = _script()
    items = module.ROLLING_ITEMS
    assert len(items) == module.ROLLING_MAX_ITEMS + 1
    kept, incoming = items[: module.ROLLING_MAX_ITEMS], items[-1]
    lowest = min(kept, key=lambda item: item[2])
    # The eviction plan never trades a better item for a worse one.
    assert incoming[2] > lowest[2]


def test_the_rolling_corpus_carries_a_key_the_list_reads_as_implicit():
    import uuid

    from app.services.builtin_tools.workset import chat_workset_key, is_implicit_workset_key

    assert is_implicit_workset_key(chat_workset_key(uuid.uuid4()))


def test_the_seed_writes_an_implicit_and_a_sealed_workset():
    source = SCRIPT.read_text(encoding="utf-8")
    seed = ast.unparse(
        next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "seed")
    )
    for part in (
        "key=chat_workset_key(agent)",
        "implicit=True",
        "max_items=ROLLING_MAX_ITEMS",
        "the implicit corpus evicted nothing",
        "await worksets.seal(db, sealed)",
        "str(rolling.id), str(sealed.id)",
    ):
        assert part in seed, part


def test_the_lab_logs_the_four_worksets():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    helper = ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "seed_operations"))
    assert "worksets (active, resolved, rolling, sealed)" in helper
