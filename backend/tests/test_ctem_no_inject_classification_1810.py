"""#1810: the CTEM fleets read an inject like an analyst; the platform never classifies it.

No seeded persona, assignment prompt or CTEM tool names the removed classifier, the
read-only / intrusive class or safe mode, and ``record_validation`` stores the agent's
``conclusive`` as given (a pass the agent calls inconclusive is stored inconclusive).
OpenAEV's own workflow safe mode (the attack-path persona) is an upstream feature and
stays out of this check.
"""

from __future__ import annotations

import importlib.util
import inspect
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.agents import builtin_agents
from app.models.ctem_case import CtemCase
from app.services import ctem_case_loop as loop
from app.services.builtin_tools import ctem_cases as ctem_tools

PERSONAS = Path(builtin_agents.__file__).parent / "seed_data" / "agent_persona"
#: OpenAEV's attack-chaining workflow safe mode is its own setting, not ours (#1810 leaves it).
UPSTREAM = {"openaev-attack-path-orchestrator.md"}
FORBIDDEN = re.compile(
    r"assess_ctem_validation_inject|inject_class|safe[_ -]mode|read[_-]only (inject|class|check)|intrusive",
    re.IGNORECASE,
)
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)


def _ctem_personas() -> list[Path]:
    return [p for p in sorted(PERSONAS.glob("ctem-*.md")) if p.name not in UPSTREAM]


def test_no_ctem_persona_mentions_the_classifier_or_safe_mode() -> None:
    personas = _ctem_personas()
    assert personas, "the CTEM personas moved; point this test at them"
    hits = {p.name: m.group(0) for p in personas if (m := FORBIDDEN.search(p.read_text(encoding="utf-8")))}
    assert hits == {}


def test_no_seeded_assignment_prompt_mentions_the_classifier_or_safe_mode() -> None:
    hits = [
        a.get("name")
        for a in builtin_agents.BUILTIN_ASSIGNMENTS
        if FORBIDDEN.search(str(a.get("prompt") or "") + str(a.get("description") or ""))
    ]
    assert hits == []


def test_the_classifier_tool_and_module_are_gone() -> None:
    assert importlib.util.find_spec("app.services.ctem_inject_effects") is None
    assert "assess_ctem_validation_inject" not in inspect.getsource(ctem_tools)
    params = inspect.signature(loop.record_validation).parameters
    assert "safe_mode" not in params and "inject_class" not in params


def _case() -> CtemCase:
    return CtemCase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        vertical="cyber",
        title="Harden the dashboard server",
        remediation_kind="config",
        keys=["config:dashboard"],
        pinned_issues=[],
        state="remediating",
        state_since=NOW,
        validations=[],
    )


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)


@pytest.mark.asyncio
@pytest.mark.parametrize("conclusive", [True, False])
async def test_the_agents_conclusive_is_stored_as_given(conclusive: bool) -> None:
    case = _case()
    await loop.record_validation(
        _Session(),  # type: ignore[arg-type]
        case,
        provider="openaev",
        kind="validation",
        ref="sim-1",
        result="passed",
        conclusive=conclusive,
        now=NOW,
    )
    entry = case.validations[-1]
    assert entry["conclusive"] is conclusive
    assert entry["result"] == ("passed" if conclusive else "inconclusive")
    assert "safe_mode" not in entry and "inject_class" not in entry
