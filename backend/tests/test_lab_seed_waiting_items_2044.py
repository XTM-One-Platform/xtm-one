"""#2044: the xtm-one lab seeds items waiting for the administrator in the company and fleet scopes.

The "Waiting for me" chip of the work items list (#1506) counts the scope of the page: without a
company-managed and a fleet-workspace waiting item, it never renders a count on those scopes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")


def _functions() -> dict[str, ast.FunctionDef]:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def test_the_seed_writes_both_scopes_and_reads_them_back_through_waiting_for_me():
    helper = ast.unparse(_functions()["seed_waiting_items"])
    for part in (
        "'needs_human': True",
        "'recommendation': 'A'",
        "'is_company_managed': True",
        "'correlation_key': 'lab-waiting-company'",
        "'correlation_key': 'lab-waiting-fleet'",
        "'workspace_id': workspace",
        "f'/fleets/{fleet}'",
        "waiting_for_me=true",
        "&fleet={fleet}",
        "'waiting company item'",
        "'waiting fleet item'",
    ):
        assert part in helper, part
    # No owner agent: no inbox claims the seeded rows.
    assert "owner_agent_id" not in helper


def test_the_lab_calls_the_seed_after_the_locked_fleet_and_skips_on_a_refusal():
    seeded = ast.unparse(_functions()["seed_entities"])
    call = "seed_waiting_items(token, base, seeded.get('locked fleet', {}).get('id'))"
    assert call in seeded and "skipped['waiting items']" in seeded
    assert seeded.index("seed_locked_fleet(") < seeded.index(call) < seeded.index("seeded.json")
