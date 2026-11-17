"""The invariant behind exempting ``transfer_to_agent``.

``transfer_to_agent`` is on the shipped whitelist, so it runs with no human in
the loop. That is only safe because of one property: the tool routes but does
not execute, and the agent it routes to is gated independently. Take either half
away and the exemption becomes a hole — an ungated call that reaches a gated
tool through a specialist.

Neither half was pinned anywhere. This file pins both, because the failure mode
is silent: a refactor that ran the handover target inline, or without installing
a gate, would leave every test green while quietly un-gating every tool the
target can call.

These read source rather than driving a chat turn. That is deliberate — the
property being protected is structural ("this code path installs a gate"), a
full handover needs an LLM and two agents, and a scrape that breaks is loud
whereas an integration test that silently stops covering the interesting branch
is not. Each check guards its own scrape so a moved function cannot make the
assertion vacuous.
"""

from pathlib import Path

import pytest

from app.services.hitl_whitelist_default import (
    DELIBERATELY_EXCLUDED,
    SHIPPED_DEFAULT_WHITELIST,
)

_BACKEND = Path(__file__).resolve().parents[1]
_AGENT_CHAT = _BACKEND / "app" / "services" / "agent_chat.py"
_GENERAL_CHAT = _BACKEND / "app" / "services" / "general_chat.py"


def _function_body(path: Path, signature: str, end_marker: str) -> str:
    """Source between a function's ``def`` line and a marker that follows it.

    An explicit end marker rather than "the next def at this indent": these are
    closures nested several levels deep, and an indent-based scan walked past
    the end of the function into unrelated code — which made the first version
    of this file fail on a `run_agent_chat` mention in someone else's comment.
    A wrong boundary here is worse than no test: it fails for a reason that has
    nothing to do with the property being protected.
    """
    src = path.read_text(encoding="utf-8")
    start = src.index(signature)
    end = src.index(end_marker, start)
    body = src[start:end]
    assert 100 < len(body) < 4000, (
        f"scrape of {signature} looks wrong ({len(body)} chars) — check the "
        f"end marker {end_marker!r}"
    )
    return body


def test_transfer_to_agent_is_exempt():
    """The entry this file exists to justify."""
    assert "transfer_to_agent" in SHIPPED_DEFAULT_WHITELIST


def test_the_handover_target_is_gated():
    """Half one: whoever runs the transferred-to agent installs an approval gate.

    ``run_agent_chat`` is the single door a handover target goes through — from
    the General Assistant's transfer and from an agent-to-agent handover alike.
    If it stops building a gate, every tool the specialist calls runs
    unattended, reached by a call nobody approved.
    """
    src = _AGENT_CHAT.read_text(encoding="utf-8")
    assert "async def run_agent_chat" in src, "run_agent_chat moved — re-point this test"
    assert "build_approval_gate" in src, (
        "run_agent_chat no longer installs an approval gate. Remove "
        "'transfer_to_agent' from the shipped whitelist: with this gone, an "
        "ungated transfer reaches every tool the target agent has."
    )


def test_the_transfer_tool_itself_executes_nothing():
    """Half two: the tool records a routing decision and returns.

    It must not run the target, or the gate installed downstream would be
    installed *after* the work it was meant to precede.
    """
    body = _function_body(
        _GENERAL_CHAT,
        "async def _transfer_to_agent(",
        'ho_exec.register("transfer_to_agent"',
    )
    for forbidden in ("run_agent_chat", "run_agent_loop", "consult", "executor.execute"):
        assert forbidden not in body, (
            f"_transfer_to_agent now calls {forbidden!r}. It is whitelisted on "
            "the basis that it only records a handover; if it executes the "
            "target, the exemption un-gates that agent's tools."
        )
    assert "handover_result[" in body, (
        "the handover no longer records into handover_result — re-read what this "
        "tool does before trusting its exemption"
    )


def test_consult_agent_stays_gated():
    """The sibling that must NOT be exempted, and for the recorded reason.

    ``consult_agent`` runs its target through the same gated door, so the
    consulted agent is not the problem. The problem is that a pause raised
    inside the nested chat cannot cross the consult boundary — it is caught and
    reported as a failed consultation, so the human is never asked. Exempting
    the consult would not fix that; it would hide it.
    """
    assert "consult_agent" not in SHIPPED_DEFAULT_WHITELIST
    assert "consult_agent" in DELIBERATELY_EXCLUDED
    reason = DELIBERATELY_EXCLUDED["consult_agent"]
    assert "Consultation failed" in reason or "cannot cross" in reason, (
        "the exclusion reason no longer names why consult_agent is different "
        "from transfer_to_agent; an inaccurate reason here was already fixed "
        "once and should not drift back"
    )


@pytest.mark.parametrize(
    "surface",
    [
        _BACKEND / "app" / "api" / "v1" / "chat.py",
        _BACKEND / "app" / "api" / "v1" / "public_chat.py",
    ],
)
def test_surfaces_without_an_approval_channel_still_exist(surface):
    """Why gating a terminal routing tool was worse than slow.

    These two install a gate and have no way to answer it, so a gated call ends
    the turn with prose. That is correct for a tool that would change something
    — and was a dead end for a handoff, since the user could not grant what was
    being asked. If this stops being true, the third argument in the whitelist
    comment is stale and should be re-read rather than trusted.
    """
    assert "no_approval_channel_message" in surface.read_text(encoding="utf-8")
