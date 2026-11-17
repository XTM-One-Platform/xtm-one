"""The rollout switch must be readable where operators are told to set it.

``TOOL_APPROVALS_ENABLED`` was read with ``os.getenv``, which pydantic's
``env_file`` never populates, so it was inert on the upgrade path it was
written for. The fleet backfill it once sat beside is gone (maintainer
decision, 2026-09-27): an upgrade rewrites no agent row.
"""

import ast
import pathlib

import pytest

VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"


@pytest.fixture(autouse=True)
def _no_os_env(monkeypatch):
    """An operator who edits .env sets nothing in the process environment."""
    monkeypatch.delenv("TOOL_APPROVALS_ENABLED", raising=False)


def test_the_kill_switch_field_is_declared_so_a_dotenv_value_reaches_it():
    """Declared, so a ``.env`` value reaches it on the upgrade path too."""
    from app.core.config import Settings

    declared = set(Settings.model_fields)

    assert "tool_approvals_enabled" in declared
    assert "tool_approvals_backfill_agents" not in declared, "the backfill is gone"


def test_neither_migration_reads_the_process_environment():
    """A regression pin: reverting either to ``os.getenv`` restores a switch
    that silently does nothing."""
    for stem in ("e5rel3298", "i1seed3311"):
        source = next(VERSIONS.glob(f"{stem}*.py")).read_text(encoding="utf-8")
        # Comments and docstrings explain the defect by name; the pin is on code.
        tree = ast.parse(source)
        reads = {
            f"{n.value.id}.{n.attr}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        }
        assert "os.getenv" not in reads, f"{stem} must read through settings"
        assert "os.environ" not in reads, f"{stem} must read through settings"


def test_no_migration_rewrites_an_agent_row():
    """An upgrade switches approval on for no agent: no UPDATE of ``agents``."""
    for stem in ("e5rel3298", "j1atc0173"):
        source = next(VERSIONS.glob(f"{stem}*.py")).read_text(encoding="utf-8")
        assert "UPDATE AGENTS" not in " ".join(source.upper().split()), stem
