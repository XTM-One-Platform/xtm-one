"""Platform Health engine without a database (#101 S2): stable ids, score formula,
the pass never touches the quota layer, and the engine module imports nothing metered."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.health import engine
from app.services.health.engine import Check, finding_key, register_check, score_from_counts

ENGINE_SOURCE = Path(engine.__file__).read_text(encoding="utf-8")


def test_a_finding_id_is_stable_whatever_the_subject_key_order():
    assert finding_key("agents.tool_errors", {"agent_id": "a", "tool": "t"}) == finding_key(
        "agents.tool_errors", {"tool": "t", "agent_id": "a"}
    )
    assert finding_key("agents.tool_errors", {"agent_id": "a"}) != finding_key("agents.tool_errors", {"agent_id": "b"})
    assert len(finding_key("xtm_one.worker", {})) == 64


@pytest.mark.parametrize(
    ("critical", "warning", "info", "score"),
    [(0, 0, 0, 100), (1, 0, 0, 75), (0, 1, 3, 89), (2, 6, 0, 2), (4, 1, 0, 0)],
)
def test_the_score_is_100_minus_25_8_1_floored_at_zero(critical, warning, info, score):
    assert score_from_counts(critical, warning, info) == score


def test_a_check_of_an_unknown_module_is_refused():
    async def run(db, now):
        return []

    with pytest.raises(ValueError, match="Unknown health module"):
        register_check(Check(check_id="x.y", module="copilot", interval=engine.timedelta(minutes=5), run=run))


def test_the_engine_never_imports_the_quota_layer_or_a_run_context():
    """Not metered (section 3): nothing in the engine reaches quota, runs or a model."""
    imported: set[str] = set()
    for node in ast.walk(ast.parse(ENGINE_SOURCE)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    forbidden = {"app.services.quota", "app.services.agentic_execution", "app.agents.run_context", "app.llm"}
    assert not {name for name in imported if any(name.startswith(f) for f in forbidden)}
    names = {node.id for node in ast.walk(ast.parse(ENGINE_SOURCE)) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(ast.parse(ENGINE_SOURCE)) if isinstance(node, ast.Attribute)}
    assert not names & {"increment_agentic_usage", "check_agentic_quota", "RunContext", "AssignmentRun"}
