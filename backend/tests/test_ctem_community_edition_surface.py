"""The CTEM command center works in Community Edition with AI disabled (#9194).

XTM One mode without a licence and with AI switched off is meant to be an
open-source CTEM command center: every figure computable without a model is
shown and refreshable. The posture surface is deterministic (HTTP aggregates
from OpenCTI / OpenAEV / OpenCRQ), so no AI or licence gate may sit on it, and
nothing on it may reach a model or start an agent run.

The AI surfaces of the same router (the exposure briefing and stage
assessment refreshes, the analyst overrides) keep their gates: they start a
run or configure one.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parent.parent
CTEM_ROUTER = BACKEND / "app" / "api" / "v1" / "ctem_metrics.py"

#: Gates that refuse with AI off or without a licence.
_AI_OR_LICENCE_GATES = {
    "require_ai_enabled",
    "require_agentic_ai",
    "require_agentic_ai_licensed",
    "require_chat_enabled",
}

#: The deterministic posture surface: no gate, no model, no run.
_DETERMINISTIC_HANDLERS = {
    "ctem_posture",
    "ctem_posture_history",
    "ctem_posture_movers",
    "ctem_stage_detail",
    "refresh_ctem_posture",
}

#: The AI artifact reads: ungated (they answer a status), never a run.
_AI_ARTIFACT_READS = {
    "ctem_briefing",
    "ctem_stage_assessment",
}

#: The AI surfaces that must stay gated (they start or configure a run).
_GATED_HANDLERS = {
    "refresh_my_briefing",
    "refresh_stage_assessment",
}

#: Calls that mean model traffic or agentic work.
_AI_CALL_HINTS = (
    "trigger_assignment",
    "get_llm_for_user",
    "get_llm(",
    "embed_text",
    "embed_texts",
    "PROVIDER_MAP",
    "create_execution",
)


def _handlers() -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    tree = ast.parse(CTEM_ROUTER.read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }


def _depends_on(node: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for default in node.args.defaults + list(node.args.kw_defaults):
        if isinstance(default, ast.Call) and getattr(default.func, "id", "") == "Depends":
            for arg in default.args:
                names.add(getattr(arg, "id", None) or getattr(arg, "attr", None) or "")
    return names


def _called_names(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name:
                found.add(name)
    return found


def _reachable_module_functions(root: str) -> set[str]:
    """``root`` and every function of the router module it calls, transitively."""
    functions = _handlers()
    seen: set[str] = set()
    stack = [root]
    while stack:
        name = stack.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        stack.extend(_called_names(functions[name]) - seen)
    return seen


def test_the_handlers_named_here_exist():
    """A rename must fail loudly rather than turn the tripwire into a no-op."""
    handlers = _handlers()
    missing = (_DETERMINISTIC_HANDLERS | _GATED_HANDLERS | _AI_ARTIFACT_READS) - set(handlers)
    assert not missing, f"rename the entries of this test: {sorted(missing)}"


def test_the_posture_surface_carries_no_ai_or_licence_gate():
    handlers = _handlers()
    offenders = {
        name: sorted(_depends_on(handlers[name]) & _AI_OR_LICENCE_GATES)
        for name in _DETERMINISTIC_HANDLERS
        if _depends_on(handlers[name]) & _AI_OR_LICENCE_GATES
    }
    assert not offenders, (
        "the CTEM posture is deterministic and must answer with AI off and "
        f"without a licence (Community Edition): {offenders}"
    )


def test_the_posture_surface_reaches_no_model_and_starts_no_run():
    functions = _handlers()
    offenders: dict[str, list[str]] = {}
    for name in _DETERMINISTIC_HANDLERS | _AI_ARTIFACT_READS:
        for fn in sorted(_reachable_module_functions(name)):
            source = ast.unparse(functions[fn])
            hits = [h for h in _AI_CALL_HINTS if h in source]
            if hits:
                offenders.setdefault(name, []).append(f"{fn}: {hits}")
    assert not offenders, (
        "a Community Edition path must not call a model or start an agentic "
        f"run: {offenders}"
    )


def test_the_collector_reaches_no_model():
    """The posture build lives in the collector; it must stay model-free."""
    source = (BACKEND / "app" / "services" / "ctem_metrics_collector.py").read_text(
        encoding="utf-8"
    )
    hits = [h for h in _AI_CALL_HINTS if h in source]
    assert not hits, f"ctem_metrics_collector must stay deterministic: {hits}"


def test_the_ai_refreshes_stay_gated():
    """S-EE must not regress: the briefing and assessment refreshes start a run."""
    handlers = _handlers()
    ungated = sorted(
        name for name in _GATED_HANDLERS
        if not (_depends_on(handlers[name]) & _AI_OR_LICENCE_GATES)
    )
    assert not ungated, f"these refreshes start an agent run and must stay gated: {ungated}"


def test_the_artifact_reads_answer_why_they_cannot_generate():
    """B1/B2 (#9207): the UI auto-generates on ``pending``; with AI off or no
    model the reads must answer ``ai_disabled`` / ``no_model`` instead."""
    handlers = _handlers()
    missing = sorted(
        name for name in _AI_ARTIFACT_READS
        if "_ai_lock_status" not in _called_names(handlers[name])
    )
    assert not missing, f"these reads must resolve the AI lock before 'pending': {missing}"


# ── _ai_lock_status behaviour (AI off > no model > none) ─────────────────────


def _lock(monkeypatch, *, ai_on: bool, agent_model: str | None, default_model: str | None):
    from app.api.v1 import ctem_metrics, platform_settings_helpers
    from app.services import ai_state

    async def _ai(_db):
        return ai_on

    async def _ps(_db):
        return SimpleNamespace(agent_default_model=default_model)

    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai)
    monkeypatch.setattr(platform_settings_helpers, "peek_platform_settings", _ps)
    agent = SimpleNamespace(llm_model=agent_model)
    return asyncio.run(ctem_metrics._ai_lock_status(None, agent))


@pytest.mark.parametrize(
    ("ai_on", "agent_model", "default_model", "expected"),
    [
        (False, "m", "m", "ai_disabled"),
        (True, None, None, "no_model"),
        (True, None, "", "no_model"),
        (True, None, "gpt", None),
        (True, "claude", None, None),
    ],
)
def test_ai_lock_status(monkeypatch, ai_on, agent_model, default_model, expected):
    assert _lock(
        monkeypatch, ai_on=ai_on, agent_model=agent_model, default_model=default_model
    ) == expected
