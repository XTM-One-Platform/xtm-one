"""#61: the xtm-one lab seeds a knowledge base activity log with every event kind.

The plan is checked against the event kinds ``KBActivityLog.event`` documents and the event types of
the Activity filter (``ACTIVITY_EVENT_TYPES``), and the lab wiring (script, read-back, log line,
skip) is read from ``lab.py``, so a change that would leave the Activity tab short in a lab fails here.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_kb_activity.py"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_seed_kb_activity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _documented_events() -> set[str]:
    from app.models import knowledge_base

    source = inspect.getsource(knowledge_base.KBActivityLog)
    block = source.split("# Activity type:", 1)[1].split("event:", 1)[0]
    return set(re.findall(r"[a-z_]+", block.replace("#", " ")))


def test_the_plan_writes_every_documented_event_kind():
    module = _module()
    rows = module.plan(datetime.now(UTC))
    kinds = {row["event"] for row in rows}
    assert _documented_events() <= kinds
    assert kinds == {event for event, _, _ in module.EVENTS}


def test_the_plan_fills_more_than_one_page_with_a_long_line():
    module = _module()
    rows = module.plan(datetime.now(UTC))
    assert len(rows) == module.TOTAL > 50
    assert any(len(row["details"]) > 300 for row in rows)
    assert all(len(row["event"]) <= 50 for row in rows)  # KBActivityLog.event is String(50)
    assert all(row["source_type"] is None or len(row["source_type"]) <= 30 for row in rows)
    times = [row["created_at"] for row in rows]
    assert times == sorted(times, reverse=True) and len(set(times)) == len(times)


def test_every_filter_event_type_has_rows():
    from app.api.v1.knowledge_bases import ACTIVITY_EVENT_TYPES

    module = _module()
    events = [row["event"] for row in module.plan(datetime.now(UTC))]
    for key, prefixes in ACTIVITY_EVENT_TYPES.items():
        assert any(event.startswith(prefixes) for event in events), key


def test_the_lab_starts_the_seed_reads_it_back_and_logs_it():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_kb_activity"])
    for part in (
        "'seed_kb_activity.py'",
        "sys.executable",
        "env=seed_script_env()",
        "/activity/count",
        "total <= 50",
        "('sync', 'documents', 'embeddings')",
        "seeded KB activity:",
        "?tab=activity",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_kb_activity(token, base)"
    assert call in seeded and "skipped['KB activity']" in seeded
    assert seeded.index(call) < seeded.index("seeded.json")
