"""#10734: ``LAB_FIGURES_SCALE=large`` seeds fleet and home figures of 1,000 and more.

The quota lines go through the product's own ``fleet_docs.clean_structured`` and the runs through
``fleet_views.run_stats``, so a change that would refuse the lines or leave the figures under 1,000
fails here, not in a compact-numbers UI review. The services are the default and Copilot labs
with the switch.
"""

from __future__ import annotations

import ast
import importlib.util
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_large_figures.py"
CHECKS = ROOT / ".xtm-one" / "checks.toml"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_seed_large_figures", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_each_fleet_quota_is_valid_over_1000_and_reads_about_floor_range():
    from app.services.fleet_docs import clean_structured

    module = _module()
    shapes = []
    for index in range(3):
        lines = module.quota_lines(index)
        cleaned = clean_structured({"quota": lines})
        assert len(cleaned["quota"]) == len(lines), index
        low, high = module.quota_total(lines)
        assert low >= 1000 and high >= low, index
        open_ended = any(q.get("open_ended") for q in lines)
        shapes.append("floor" if open_ended else "about" if low == high else "range")
    assert shapes == ["about", "floor", "range"]


def test_the_fleet_runs_count_over_1000_in_the_about_window():
    from app.services.fleet_views import run_stats

    module = _module()
    now = datetime.now(UTC)
    rows = module.run_rows("00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002", now)
    stats = run_stats(((r["created_at"], r["status"], r["error"]) for r in rows), awaiting_approval=0, now=now)
    assert stats.runs == module.FLEET_RUNS > 1000
    assert 0 < stats.failed < stats.runs
    for count in (module.HOME_AGENTS, module.HOME_CONVERSATIONS, module.HOME_DOCUMENTS):
        assert count > 1000


def test_the_lab_starts_the_seed_under_the_switch_and_reads_the_figures_back():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_large_figures"])
    for part in (
        "'seed_large_figures.py'",
        "sys.executable",
        "env=seed_script_env()",
        "/documentation",
        "/health",
        "/activity/stats",
        "seeded large fleet and home figures: ",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_large_figures(token, base)"
    assert call in seeded and "skipped['large fleet and home figures']" in seeded
    assert "lab_switch('LAB_FIGURES_SCALE', LAB_FIGURES_SCALES) == 'large'" in seeded
    assert seeded.index(call) < seeded.index("seeded.json")


def test_the_large_figures_labs_are_the_default_labs_with_the_switch():
    services = tomllib.loads(CHECKS.read_text(encoding="utf-8"))["service"]
    twins = {
        "lab-figures-large": ("lab", {"LAB_FIGURES_SCALE": "large"}),
        "lab-figures-large-light": ("lab", {"LAB_FIGURES_SCALE": "large", "LAB_THEME": "light"}),
        "lab-copilot-large": ("lab-copilot", {"LAB_FIGURES_SCALE": "large"}),
        "lab-copilot-large-light": ("lab-copilot", {"LAB_FIGURES_SCALE": "large", "LAB_THEME": "light"}),
    }
    for name, (parent, switches) in twins.items():
        service, base = services[name], services[parent]
        assert service["env"] == {**base["env"], **switches}, name
        assert {k: v for k, v in service.items() if k not in ("env", "description")} == {
            k: v for k, v in base.items() if k not in ("env", "description")
        }, name
    assert not any("LAB_FIGURES_SCALE" in s.get("env", {}) for n, s in services.items() if n not in twins)
