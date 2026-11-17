"""#10749: ``LAB_FIGURES_SCALE=large`` seeds two fleets whose ROI figures reach 1,000 and more.

The records go through the product's own ``roi.compute_roi`` and the quota lines through
``fleet_docs.clean_structured``, so a catalog or rate change that would leave the figures under
the K and M thresholds fails here, not in a compact-numbers UI review.
"""

from __future__ import annotations

import ast
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_large_roi.py"
AGENT = "00000000-0000-0000-0000-000000000001"
FLEET = "00000000-0000-0000-0000-000000000002"
USER = "00000000-0000-0000-0000-000000000003"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_seed_large_roi", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _figures(module, count: int, units: int, spend: float = 0.0):
    from app.services import roi

    rows = module.record_rows(AGENT, FLEET, USER, count, units, datetime.now(UTC))
    measures = [
        roi.RunMeasure(unit=r["unit"], units=r["units"], llm_cost=spend / count, outcome=r["outcome"])
        for r in rows
    ]
    return roi.compute_roi(measures, currency="USD")


def test_the_large_fleet_reads_in_thousands_and_millions():
    module = _module()
    figures = _figures(module, module.LARGE_RECORDS, module.UNITS_PER_RECORD)
    assert figures.runs >= 1000 and figures.units >= 1000
    assert figures.hours_saved >= 1000
    assert figures.value >= 1_000_000
    assert figures.net_value == figures.value
    assert 0 < figures.rejected < figures.runs


def test_the_negative_fleet_net_value_is_below_minus_one_million():
    module = _module()
    spend = module.TRACE_COST * module.SPEND_TRACES
    figures = _figures(module, module.NEGATIVE_RECORDS, 1, spend)
    assert figures.runs >= 1000 and figures.units >= 1000
    assert figures.net_value <= -1_000_000
    traces = module.trace_rows(AGENT, USER, datetime.now(UTC))
    assert sum(t["total_cost"] for t in traces) == spend
    assert all(t["started_at"].date() < datetime.now(UTC).date() for t in traces)


def test_each_fleet_quota_is_valid_over_1000():
    from app.services.fleet_docs import clean_structured

    module = _module()
    for index in range(2):
        lines = module.quota_lines(index)
        assert len(clean_structured({"quota": lines})["quota"]) == len(lines), index
        assert sum(q["runs_min"] for q in lines) >= 1000, index


def test_the_lab_starts_the_seed_under_the_switch_and_reads_the_figures_back():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_large_roi"])
    for part in (
        "'seed_large_roi.py'",
        "sys.executable",
        "env=seed_script_env()",
        "/roi/declarations",
        "/documentation",
        "/roi?window=30d",
        "seeded large ROI fleet: ",
        "/flows/fleets/",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_large_roi(token, base)"
    assert call in seeded and "skipped['large ROI fleets']" in seeded
    switch = "lab_switch('LAB_FIGURES_SCALE', LAB_FIGURES_SCALES) == 'large'"
    assert seeded.index(switch) < seeded.index(call) < seeded.index("seeded.json")
