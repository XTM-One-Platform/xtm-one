"""The built-in tools release notice seed of the lab (dev:harness-gaps lab-seed-builtin-tools-notice)."""

from __future__ import annotations

import ast
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_builtin_tools_notice.py"

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")


def _script():
    spec = importlib.util.spec_from_file_location("lab_seed_builtin_tools_notice", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source(name: str) -> str:
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    return ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name))


def _catalog() -> dict[str, list[str]]:
    from app.tools.builtin_suites import builtin_suite_catalog

    return {group: list(names) for group, names in builtin_suite_catalog().items()}


def test_the_catalog_offers_a_group_to_scope():
    group, kept, left_out = _script().scope(_catalog())
    assert group in _script().GROUPS
    assert len(kept) == 1 and left_out and kept[0] not in left_out


def test_scope_refuses_when_no_group_has_two_functions():
    with pytest.raises(RuntimeError):
        _script().scope({"web": ["only"]}, ("web",))


def test_the_scoped_agent_gets_one_notice_routed_to_its_tooling_section():
    from app.services.builtin_tool_release_notice import NOTIFICATION_SOURCE, agents_left_out, by_owner, notice
    from app.services.notification_feed import NOTIFICATION_KINDS

    group, kept, left_out = _script().scope(_catalog())
    owner, agent_id = str(uuid.uuid4()), uuid.uuid4()
    row = SimpleNamespace(
        id=agent_id,
        name="Lab Scoped tools",
        user_id=owner,
        builtin_tool_ids=[group],
        builtin_allowed_tools={group: kept},
        config={},
    )
    owners = by_owner(agents_left_out({group: left_out}, [row]))
    assert list(owners) == [owner]
    _title, _summary, extra = notice(_script().RELEASE, owners[owner])
    assert extra["notification_source"] == NOTIFICATION_SOURCE
    assert NOTIFICATION_SOURCE in NOTIFICATION_KINDS
    assert extra["path"].startswith(f"/agents/{agent_id}/config/tooling")
    assert extra["agents"][0]["outside"] == {group: left_out}


def test_the_seed_reads_back_and_logs():
    helper = _source("seed_builtin_tools_notice")
    for part in (
        "'seed_builtin_tools_notice.py'",
        "'seed'",
        "sys.executable",
        "env=seed_script_env()",
        "/notifications?limit=50",
        "builtin_tools_outside_allowlist",
        "/config/tooling",
        "seeded built-in tools notice",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part


def test_every_lab_runs_the_seed():
    seed = _source("seed_entities")
    assert "seed_builtin_tools_notice(token, base)" in seed
    assert "skipped['built-in tools notice']" in seed
