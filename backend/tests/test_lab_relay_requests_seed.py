"""The lab's relay request log seed (#10763): what the rows cover and how ``lab.py`` runs the script.

The rows are built by plain functions of ``.xtm-one/lab/seed_relay_requests.py`` (no database); the
script is loaded by path. dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import importlib.util
import re
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = ROOT / ".xtm-one" / "lab" / "seed_relay_requests.py"
LABELS = ROOT / "frontend" / "src" / "pages" / "relay-group-detail" / "relay-labels.ts"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _script():
    spec = importlib.util.spec_from_file_location("lab_seed_relay_requests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_rows_cover_every_state_the_activity_tab_renders():
    script = _script()
    relays = [uuid.uuid4(), uuid.uuid4()]
    rows = script.rows(relays)
    assert 100 <= len(rows) <= 150
    assert {row["error_code"] for row in rows} - {None} == set(script.ERROR_CODES)
    statuses = {row["status_code"] // 100 for row in rows if row["status_code"]}
    assert {2, 4, 5} <= statuses
    assert any(len(row["host"]) >= 90 for row in rows)
    assert any(row["duration_ms"] >= 1000 for row in rows)
    assert {row["source_type"] for row in rows} == {"integration", "mcp_server", "flow", "test", None}
    assert any(row["relay_id"] is None for row in rows)
    assert {row["relay_id"] for row in rows} - {None} == set(relays)
    # Every host appears more than once, so the host search narrows without emptying the page.
    assert all(sum(row["host"] == host for row in rows) > 1 for host, _, _ in script.DESTINATIONS)
    # Metadata only: no header, no body, no query string.
    allowed = {
        "relay_id",
        "source_type",
        "method",
        "host",
        "port",
        "path",
        "status_code",
        "error_code",
        "bytes_sent",
        "bytes_received",
        "duration_ms",
        "created_at",
    }
    assert all(set(row) <= allowed for row in rows)
    assert all("?" not in row["path"] for row in rows)
    # A row answered by the destination has no error code, and an error row has no status.
    assert all((row["status_code"] is None) == (row["error_code"] is not None) for row in rows)


def test_the_relays_have_a_long_name_and_no_usable_key():
    script = _script()
    assert len(script.RELAYS) >= 2
    assert any(len(name) >= 40 for name, *_ in script.RELAYS)
    assert {status for _, status, *_ in script.RELAYS} == {"online", "offline"}
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'key_hash="lab-seed-placeholder-not-a-key-hash"' in source


def test_the_error_codes_are_the_frontends_labels():
    if not LABELS.is_file():
        pytest.skip("no frontend next to the backend")
    text = LABELS.read_text(encoding="utf-8")
    block = text[text.index("const RELAY_ERROR_LABELS") :]
    block = block[: block.index("}")]
    codes = set(re.findall(r"^\s+([a-z_]+):", block, re.MULTILINE))
    assert codes == set(_script().ERROR_CODES)


def test_the_lab_runs_the_script_after_the_relay_group_and_logs_the_activity_url():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_relay_requests"])
    for part in (
        "'seed_relay_requests.py'",
        "sys.executable",
        "env=seed_script_env()",
        "stderr_summary(process.stderr)",
        "/requests?limit=1",
        "/activity",
        "seeded relay requests: {url}",
    ):
        assert part in helper, part
    seed = ast.unparse(functions["seed_entities"])
    assert (
        seed.index("for entity, route, create in SEEDS")
        < seed.index("seed_relay_requests(token, ids['relay group'], base)")
        < seed.index("seeded.json")
    )
