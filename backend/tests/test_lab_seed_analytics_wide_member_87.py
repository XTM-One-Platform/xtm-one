"""The production-shaped analytics member of the lab (#87): one user in about ninety groups."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")


def _tree() -> ast.Module:
    return ast.parse(LAB.read_text(encoding="utf-8"))


def _function(name: str) -> ast.FunctionDef:
    return next(n for n in _tree().body if isinstance(n, ast.FunctionDef) and n.name == name)


def _constant(name: str):
    for node in _tree().body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a module constant of lab.py")


def _group_names() -> list[str]:
    namespace: dict = {"ANALYTICS_WIDE_MEMBER_GROUPS": _constant("ANALYTICS_WIDE_MEMBER_GROUPS")}
    node = _function("analytics_wide_group_names")
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace["analytics_wide_group_names"]()


def test_the_member_is_in_production_scale_groups_with_unique_stripped_names():
    names = _group_names()
    assert _constant("ANALYTICS_WIDE_MEMBER_GROUPS") >= 90
    assert len(names) == _constant("ANALYTICS_WIDE_MEMBER_GROUPS")
    assert len(set(names)) == len(names)
    # POST /groups strips the name and refuses a duplicate: a name it would change is never the one read back.
    assert all(name == name.strip() and name for name in names)
    uuid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    assert sum(bool(uuid.match(name)) for name in names) == len(names) // 3


def test_the_member_is_the_first_analytics_user_who_owns_traces():
    users = _constant("ANALYTICS_USERS")
    assert users >= 1
    assert _constant("ANALYTICS_WIDE_MEMBER_EMAIL") == "lab-analytics-01@filigran.io"
    assert "lab-analytics-{n:02d}@filigran.io" in ast.unparse(_function("seed_analytics_traces"))


def test_the_seed_creates_the_groups_adds_the_member_reads_back_and_logs():
    helper = ast.unparse(_function("seed_analytics_wide_member"))
    for part in (
        "'analytics_ids.json'",
        "'/groups'",
        "/members",
        "'/traces/analytics/cost-by-user?period=30d'",
        "'group_names'",
        "seeded analytics wide member",
        "raise RuntimeError",
    ):
        assert part in helper, part


def test_the_large_figures_switch_runs_it_after_the_analytics_traces():
    seed = ast.unparse(_function("seed_entities"))
    assert "seed_analytics_wide_member(token, base)" in seed
    assert "skipped['analytics wide member']" in seed
    position = seed.index("seed_analytics_wide_member(token, base)")
    assert seed.index("seed_analytics_traces(token, base)") < position
    switch = seed.rindex("lab_switch('LAB_FIGURES_SCALE', LAB_FIGURES_SCALES) == 'large'", 0, position)
    assert seed.index("seed_large_roi(token, base)", switch) < position
