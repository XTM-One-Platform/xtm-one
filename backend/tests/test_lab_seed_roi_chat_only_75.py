"""The chat-only ROI window seed of the lab (dev:harness-gaps lab-roi-chat-only-window-seed)."""

from __future__ import annotations

import ast
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_roi_chat_only.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")


def _script():
    spec = importlib.util.spec_from_file_location("lab_seed_roi_chat_only", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(name: str) -> str:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    return ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name))


def _plan(now: datetime) -> tuple[list[dict], str, str]:
    agent, fleet = str(uuid.uuid4()), str(uuid.uuid4())
    return _script().plan(now, admin=str(uuid.uuid4()), agent=agent, fleet=fleet), agent, fleet


def test_every_line_is_counted_inside_the_seven_day_window():
    from app.services import roi_chat

    now = datetime.now(UTC)
    rows, _agent, _fleet = _plan(now)
    assert len(rows) == 4
    for row in rows:
        record = row["record"]
        assert record["kind"] in roi_chat.KINDS
        assert record["created_at"] > now - timedelta(days=7)
        assert roi_chat.is_counted(record["signals"], last_edit_at=record["last_edit_at"], now=now)


def test_two_lines_have_no_agent_and_two_the_agent_with_its_fleet():
    rows, agent, fleet = _plan(datetime.now(UTC))
    no_agent = [r["record"] for r in rows if r["record"]["agent_id"] is None]
    on_agent = [r["record"] for r in rows if r["record"]["agent_id"] == agent]
    assert len(no_agent) == 2 and all(r["fleet_id"] is None for r in no_agent)
    assert len(on_agent) == 2 and all(r["fleet_id"] == fleet for r in on_agent)


def test_the_ledger_answers_a_no_agent_remainder_and_a_chat_only_agent_and_fleet():
    from app.services import roi_chat, roi_figures

    now = datetime.now(UTC)
    rows, agent, fleet = _plan(now)

    def _id(value: str | None) -> uuid.UUID | None:
        return uuid.UUID(value) if value else None

    facts = [
        roi_figures.chat_fact(
            day=r["record"]["created_at"].date(),
            person_id=_id(r["record"]["user_id"]),
            agent_id=_id(r["record"]["agent_id"]),
            fleet_id=_id(r["record"]["fleet_id"]),
            unit=roi_chat.UNIT_BY_KIND[r["record"]["kind"]],
            minutes=roi_chat.estimate(r["record"]["kind"], r["record"]["size"], user_min=0.0).counted_min,
            rate=None,
        )
        for r in rows
    ]
    admin = roi_figures.Viewer(user_id=_id(rows[0]["record"]["user_id"]), organization=True)
    first = (now - timedelta(days=7)).date()
    org = roi_figures.compute(
        facts, scope=roi_figures.SCOPE_ORGANIZATION, viewer=admin, window_first=first, window_last=now.date()
    )
    remainder = next(r for r in org.by_agent if r.key == roi_figures.ROW_NO_AGENT)
    assert remainder.totals.deliverables == 2 and remainder.totals.minutes_likely > 0
    for scope, scope_id in ((roi_figures.SCOPE_AGENT, _id(agent)), (roi_figures.SCOPE_FLEET, _id(fleet))):
        figures = roi_figures.compute(
            facts, scope=scope, viewer=admin, scope_id=scope_id, window_first=first, window_last=now.date()
        )
        assert figures.totals.runs == 0 and figures.totals.deliverables == 2


def test_the_seed_reads_back_and_logs():
    helper = _source("seed_roi_chat_only")
    for part in (
        "'seed_roi_chat_only.py'",
        "sys.executable",
        "env=seed_script_env()",
        "/roi?window=7d&currency=USD&scope=platform",
        "'no_agent'",
        "seeded ROI chat-only",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part


def test_every_lab_runs_the_seed():
    seed = _source("seed_entities")
    assert "seed_roi_chat_only(token, base)" in seed
    assert "skipped['ROI chat-only']" in seed
