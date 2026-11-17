"""#11257: every lab seeds agent-tagged chat sidebar conversations over several days.

The plan is a pure function of ``seed_chat_sidebar.py``: a change that drops a date group, an
agent icon kind, the long title or the Slack and Teams rows fails here, not in a UX round.
"""

from __future__ import annotations

import ast
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_chat_sidebar.py"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_seed_chat_sidebar", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_agents_cover_emoji_named_default_and_empty_icons():
    module = _module()
    icons = [icon for _, icon in module.AGENTS]
    assert "" in icons and "Bot" in icons
    assert any(icon and not icon.isascii() for icon in icons)
    assert any(icon.isascii() and icon.isalpha() and icon != "Bot" for icon in icons)
    assert all(len(icon) <= 50 for icon in icons)


#: Fixed instants: midnight itself, just after it (the 00:00 to 00:44 UTC window where rows used to
#: cross into the day before), noon and just before the next midnight.
INSTANTS = [
    datetime(2026, 11, 12, h, m, s, tzinfo=UTC)
    for h, m, s in ((0, 0, 0), (0, 0, 30), (0, 5, 0), (0, 44, 0), (12, 0, 0), (23, 55, 0))
]


@pytest.mark.parametrize("now", INSTANTS, ids=lambda now: now.strftime("%H%M%S"))
def test_the_conversations_span_today_yesterday_and_the_previous_seven_days(now):
    module = _module()
    plan = module.conversation_plan(now)
    assert len(plan) >= 10
    days = [(now.date() - entry["at"].date()).days for entry in plan]
    assert days == [days_ago for _, _, days_ago, _ in module.CONVERSATIONS]
    assert {0, 1} <= set(days) and any(2 <= d <= 7 for d in days) and max(days) <= 7
    stamps = [entry["at"] for entry in plan]
    assert all(at <= now for at in stamps)
    titles = [entry["title"] for entry in plan]
    assert len(set(titles)) == len(titles) and all(t.startswith(module.PREFIX) for t in titles)
    assert any(len(t) > 80 for t in titles)
    channels = [entry["channel"] for entry in plan]
    assert channels.count("slack") == 1 and channels.count("teams") == 1
    assert {entry["agent"] for entry in plan} == set(range(len(module.AGENTS)))


def test_every_lab_runs_the_seed_and_reads_the_agents_back_on_the_list():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_chat_sidebar"])
    for part in ("'seed_chat_sidebar.py'", "sys.executable", "env=seed_script_env()", "/chat/conversations?limit=200",
                 "agent_name", "seeded chat sidebar conversations: ", "stderr_summary(process.stderr)"):
        assert part in helper, part
    seeded = ast.unparse(functions["seed_entities"])
    assert "seed_chat_sidebar(token, base)" in seeded and "skipped['chat sidebar conversations']" in seeded
