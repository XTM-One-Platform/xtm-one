"""The CTEM screens states ``[service.lab-ctem-screens]`` seeds after its cases (#2316).

``seed_ctem_screens_states.py`` is loaded by path and its plan checked against the models and readers the
screens use; ``lab.py`` is read from its syntax tree. dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB_DIR = ROOT / ".xtm-one" / "lab"
LAB = LAB_DIR / "lab.py"
SEED = LAB_DIR / "seed_ctem_screens_states.py"
SAMPLE = LAB_DIR / "ctem_sample.py"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SEED.is_file(), reason="no .xtm-one next to the backend")

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_signals_cover_the_admin_watchlist_and_the_peer_and_fit_the_model():
    from app.models.ctem_signal import SIGNAL_STATES, TRIGGER_PATHS, CtemSignal

    module = _load(SEED, "lab_seed_ctem_screens_states_under_test")
    rows = module.signal_rows("admin-id", "peer-id", NOW)
    columns = set(CtemSignal.__table__.columns.keys())
    assert all(set(row) <= columns for row in rows)
    assert all(row["state"] in SIGNAL_STATES and row["trigger_path"] in TRIGGER_PATHS for row in rows)
    by_owner = {(row["recorded_by"], row["state"]) for row in rows}
    assert ("admin-id", "watchlist") in by_owner
    assert {state for who, state in by_owner if who == "peer-id"} >= {"watchlist", "new"}
    # Stable keys: a restart finds every row by its dedupe key.
    assert len({row["dedupe_key"] for row in rows}) == len(rows)
    for row in rows:
        if row["state"] == "watchlist":
            assert row["decision"]["outcome"] == "watchlist" and row["decision"]["reason_code"]


def test_no_peer_leaves_the_peer_rows_out():
    module = _load(SEED, "lab_seed_ctem_screens_states_no_peer")
    rows = module.signal_rows("admin-id", None, NOW)
    assert rows and {row["recorded_by"] for row in rows} == {"admin-id"}


def test_version_events_read_back_as_a_history():
    from app.services import ctem_case_version_history as history

    module = _load(SEED, "lab_seed_ctem_screens_states_versions")
    events = [SimpleNamespace(**event) for event in module.version_events(NOW)]
    assert [e.kind for e in events] == [history.KIND, history.KIND]
    read = history.history_of(events)
    assert len(read) == 2 and read[0]["changes"], read
    assert read[1]["summary"].startswith("baseline")
    assert history.inventory_asset_count(events)["asset_count"] == 2


def test_the_skipped_run_is_what_the_stage_card_reads():
    from app.api.v1.ctem_metrics import _no_module_account_modules
    from app.services.ctem_module_preflight import NO_MODULE_ACCOUNT, STAGE_NEEDS

    module = _load(SEED, "lab_seed_ctem_screens_states_run")
    assert module.SKIPPED_STAGE_SLUG in STAGE_NEEDS
    run = SimpleNamespace(**module.skipped_run("agent", "peer", NOW))
    assert _no_module_account_modules(run, NO_MODULE_ACCOUNT) == ["openaev", "opencti"]
    json.dumps(run.trigger_context)


def test_the_sample_carries_a_retest_state_and_primary_asset_links():
    from app.services.ctem_cases import SourceIssue

    sample = _load(SAMPLE, "lab_ctem_sample_states")
    retested = [i for i in sample.SCREENS_ISSUES if i.get("retest_status")]
    assert retested and any(i.get("retest_pending") for i in retested)
    for raw in retested:
        SourceIssue(**{k: v for k, v in raw.items() if k != "days_ago"})
    assets = {a["name"] for a in sample.SCREENS_ASSETS}
    for names in sample.SCREENS_PRIMARY_ASSET_LINKS.values():
        assert set(names) <= assets


def test_lab_starts_the_states_seed_after_the_gates():
    tree = ast.parse(LAB.read_text())
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "seed_ctem_screens_states" in names
    source = LAB.read_text()
    assert source.index("seeded.update(seed_ctem_gates(token, base))") < source.index(
        "seeded.update(seed_ctem_screens_states(token, base))"
    )
