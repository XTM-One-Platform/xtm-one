"""The ROI under-k own share seed of the lab (#69)."""

from __future__ import annotations

import ast
import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_roi_under_k.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")


def _script():
    spec = importlib.util.spec_from_file_location("lab_seed_roi_under_k", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(name: str) -> str:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    return ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name))


def test_the_plan_has_two_people_each_with_runs():
    rows = _script().plan(datetime.now(UTC))
    assert {r["who"] for r in rows} == {"admin", "peer"}
    assert sum(1 for r in rows if r["who"] == "peer") == 2
    assert all(r["record"]["units"] > 0 and r["record"]["outcome"] == "accepted" for r in rows)
    assert all(r["record"]["attribution"] == "assignment" for r in rows)


def test_two_people_stay_under_the_default_k():
    from app.services import roi_figures

    people = {r["who"] for r in _script().plan(datetime.now(UTC))}
    assert len(people) < roi_figures.MIN_GROUP_SIZE_DEFAULT


def test_the_peer_reads_his_own_share_under_k():
    from app.services import roi_figures

    admin, peer, agent, fleet = (uuid.uuid4() for _ in range(4))
    rows = _script().plan(datetime.now(UTC))
    facts = [
        roi_figures.run_fact(
            day=r["record"]["created_at"].date(),
            person_id=admin if r["who"] == "admin" else peer,
            attribution="assignment",
            agent_id=agent,
            fleet_id=fleet,
            unit=r["record"]["unit"],
            units=r["record"]["units"],
            outcome="accepted",
            review_min=None,
            baseline=None,
            rate=None,
        )
        for r in rows
    ]
    viewer = roi_figures.Viewer(
        user_id=peer,
        agent_ids=frozenset({agent}),
        fleet_ids=frozenset({fleet}),
        min_group_size=roi_figures.MIN_GROUP_SIZE_DEFAULT,
    )
    for scope, scope_id in ((roi_figures.SCOPE_AGENT, agent), (roi_figures.SCOPE_FLEET, fleet)):
        figures = roi_figures.compute(
            facts,
            scope=scope,
            viewer=viewer,
            scope_id=scope_id,
            window_first=None,
            window_last=datetime.now(UTC).date(),
        )
        assert figures.suppressed is not None and figures.suppressed.own_share_only
        assert figures.totals.runs == 2


def test_the_seed_shares_reads_back_as_the_peer_and_logs():
    helper = _source("seed_roi_under_k")
    for part in (
        "'seed_roi_under_k.py'",
        "sys.executable",
        "env=seed_script_env()",
        "/roi/declarations?subject_type=fleet",
        "'shared_user_ids': [peer]",
        "PEER_PASSWORD",
        "own_share_only",
        "seeded ROI under k own share",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part


def test_every_lab_runs_the_seed_with_the_peer():
    seed = _source("seed_entities")
    assert "seed_roi_under_k(token, base, peer_id)" in seed
    assert "skipped['ROI under k own share']" in seed
