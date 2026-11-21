"""#9905: the xtm-one lab seeds one work item per ``stale_reason``.

Each planned row is run through ``work_item_stale_reason`` with the live windows, so a change to
the reasons or their windows that the seed would no longer produce fails here, not in a review.
"""

from __future__ import annotations

import ast
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_stale_reasons.py"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_seed_stale_reasons", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _item(fields: dict, now: datetime):
    from app.models.work_item import WorkItem

    item = WorkItem(id=uuid.uuid4(), user_id=uuid.uuid4(), title=fields["title"], source="agent")
    for field, value in fields.items():
        setattr(item, field, value)
    item.created_at = min(fields["last_activity_at"], now - timedelta(days=1))
    return item


def test_every_planned_row_reads_its_own_stale_reason():
    from app.services.work_item import _stale_after, _stale_ref_wait, work_item_stale_reason
    from app.services.work_item_blocked_refs import observed_deployment_repositories

    module = _module()
    now = datetime.now(UTC)
    rows = module.plan(now, _stale_after(), _stale_ref_wait(), observed_deployment_repositories())
    assert tuple(rows) == module.REASONS == ("past_due", "wait_never_releases", "ref_wait_quiet", "quiet")
    for reason, fields in rows.items():
        assert work_item_stale_reason(_item(fields, now), now) == reason, reason


def test_the_ages_follow_the_configured_windows():
    module = _module()
    now = datetime.now(UTC)
    rows = module.plan(now, timedelta(hours=72), timedelta(days=14), frozenset())
    assert now - rows["quiet"]["last_activity_at"] == timedelta(hours=72) + module.MARGIN
    assert now - rows["ref_wait_quiet"]["last_activity_at"] == timedelta(days=14) + module.MARGIN
    # A ref wait disabled falls back to the inactivity window.
    fallback = module.plan(now, timedelta(hours=24), None, frozenset())
    assert now - fallback["ref_wait_quiet"]["last_activity_at"] == timedelta(hours=24) + module.MARGIN


def test_a_window_disabled_or_the_repository_observed_raises():
    module = _module()
    now = datetime.now(UTC)
    with pytest.raises(RuntimeError, match="stale_after_hours"):
        module.plan(now, None, timedelta(days=7), frozenset())
    with pytest.raises(RuntimeError, match="work_item_deployment_repositories"):
        module.plan(now, timedelta(hours=24), None, frozenset({module.UNOBSERVED_REPO}))


def test_the_lab_starts_the_seed_and_reads_every_reason_back():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_stale_reasons"])
    for part in (
        "'seed_stale_reasons.py'",
        "sys.executable",
        "env=seed_script_env()",
        "stale_only=true",
        "correlation_key_prefix=lab-stale-reasons",
        "'stale_reason'",
        "'stale reasons'",
        "/tasks/work-items?view=stale",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    assert _module().KEY_PREFIX == "lab-stale-reasons"
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_stale_reasons(token, base)"
    assert call in seeded and "skipped['stale reasons']" in seeded
    assert seeded.index(call) < seeded.index("seeded.json")
