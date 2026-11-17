"""WS5 - the TRUSTED autonomous run writes AGENT-LEVEL memory; ordinary chats don't.

Two seams are pinned here:

1. ``_derive_memory_scope_id`` - the pure scope decision inside ``run_agent_chat``.
   AGENT-LEVEL scope (``None``) is authorized by a two-factor gate that mirrors
   agent-level tradecraft writes: BOTH the designated-writer identity
   (``is_tradecraft_writer_agent`` -> the OpenAEV orchestrator) AND a trusted
   autonomous run (``in_autonomous_run_context()``, set UNCONDITIONALLY around
   the orchestrator's ``run_agent_chat`` call). Only then do its durable
   ``learned`` / ``fact`` writes and the end-of-turn consolidation land in the
   shared pool that ``load_persistent_context`` injects into every user's session
   and the admin Memory tab reads. The run factor is the whole-call
   ``in_autonomous_run_context()``, NOT the perimeter-gated
   ``in_tradecraft_run_context()``: the tradecraft context is established only
   once the run's scope has resolved (it must carry target names for the write
   guard), so it is legitimately False on a first cycle / OpenAEV outage - and
   scoping the orchestrator's own memory to the launching operator for those
   cycles was the WS5 regression this follow-up closes. Everyone else identified
   gets the per-user scope: an ORDINARY user chatting with a company-managed
   (shared) agent, AND a specialist consulted mid-run (the consult path re-enters
   ``run_agent_chat`` in the same task and INHERITS the run-context ContextVar, so
   identity - not the context alone - is what keeps that specialist off the shared
   pool and out of its own never-purged unscoped pool). Gating agent-level on bare
   ``is_company_managed`` made every shared-agent chat write agent-level (a
   memory-poisoning / cross-user leak); agent-level is now an authorization
   decision, not a config-flag side effect.

2. The Payload Creator builtin def disables every memory tier. It is
   consulted/delegated INSIDE another agent's run under an OPERATOR user id, so
   any durable write it made polluted that operator's per-user memory. Durable,
   cross-run learning is the orchestrator's job via target-free tradecraft.
"""

from __future__ import annotations

from app.services.agent_chat import _derive_memory_scope_id

_ORCH_SLUG = "openaev-attack-path-orchestrator"
_PC_SLUG = "openaev-payload-creator"

_MEMORY_FLAGS = (
    "memory_short_term_enabled",
    "memory_medium_term_enabled",
    "memory_persistent_enabled",
    "memory_user_enabled",
)


# ── _derive_memory_scope_id ──────────────────────────────────────────────────


def test_designated_writer_in_trusted_run_scopes_memory_agent_level():
    # The orchestrator is the designated agent-level writer AND runs inside a
    # trusted autonomous run, so both factors are True: the scope MUST be None
    # (agent-level, the shared pool), never the launching operator's user id.
    scope = _derive_memory_scope_id(
        is_anonymous_user=False,
        is_agent_level_writer=True,
        is_trusted_run=True,
        conversation_id="conv-1",
        user_id="operator-uuid",
    )
    assert scope is None


def test_designated_writer_scopes_agent_level_before_perimeter_resolves():
    # THE FIRST-CYCLE / OUTAGE REGRESSION (Copilot): the memory
    # SCOPE must NOT depend on the perimeter-gated tradecraft-write context.
    # run_autonomous_cycle only calls set_tradecraft_run_context once the scope
    # resolves (scope_view_is_usable), so on a first cycle / OpenAEV outage
    # in_tradecraft_run_context() is False - yet the orchestrator's own durable
    # memory must still land agent-level from cycle 0 (WS5). The orchestrator
    # therefore establishes a SEPARATE, unconditional autonomous-run marker, and
    # the scope reads THAT. Exercise the real context helpers so this pins the
    # actual runtime seam, not a re-implementation.
    from app.services.persistent_memory import (
        in_autonomous_run_context,
        in_tradecraft_run_context,
        reset_autonomous_run_context,
        set_autonomous_run_context,
    )

    token = set_autonomous_run_context()
    try:
        # No tradecraft context was set (unresolved perimeter): the WRITE gate is
        # closed, but the run is still a trusted autonomous run.
        assert in_tradecraft_run_context() is False
        assert in_autonomous_run_context() is True
        scope = _derive_memory_scope_id(
            is_anonymous_user=False,
            is_agent_level_writer=True,
            is_trusted_run=in_autonomous_run_context(),
            conversation_id="conv-1",
            user_id="operator-uuid",
        )
        assert scope is None
    finally:
        reset_autonomous_run_context(token)
    # Marker cleared once the run ends: an ordinary chat afterwards is per-user.
    assert in_autonomous_run_context() is False


def test_company_managed_chat_agent_scopes_memory_per_user():
    # THE LEAK THIS PR CLOSES: an ordinary identified user chatting with a
    # company-managed (shared) agent is not the designated writer and not in a
    # trusted run, so the scope is the user's OWN id - never the agent-level pool
    # that load_persistent_context injects into every other user's session.
    # The helper is intentionally agnostic to is_company_managed now; a shared
    # agent's ordinary chat turn is simply a non-authorized, non-trusted run.
    scope = _derive_memory_scope_id(
        is_anonymous_user=False,
        is_agent_level_writer=False,
        is_trusted_run=False,
        conversation_id="conv-1",
        user_id="alice-uuid",
    )
    assert scope == "alice-uuid"


def test_consulted_specialist_in_trusted_run_scopes_memory_per_user():
    # THE SECOND LEAK VECTOR (both factors are why): an operator-selected
    # specialist consulted mid-run re-enters run_agent_chat in the SAME task and
    # INHERITS the run-context ContextVar, so is_trusted_run is True for it too.
    # It is NOT the designated writer, so it MUST still get the per-user scope -
    # otherwise its target-bearing summaries / store_memory writes would land in
    # its OWN agent-level pool, which the orchestrator run purge never clears.
    scope = _derive_memory_scope_id(
        is_anonymous_user=False,
        is_agent_level_writer=False,
        is_trusted_run=True,
        conversation_id="conv-1",
        user_id="operator-uuid",
    )
    assert scope == "operator-uuid"


def test_identified_private_agent_scopes_memory_per_user():
    scope = _derive_memory_scope_id(
        is_anonymous_user=False,
        is_agent_level_writer=False,
        is_trusted_run=False,
        conversation_id="conv-1",
        user_id="operator-uuid",
    )
    assert scope == "operator-uuid"


def test_anonymous_scopes_memory_to_conversation_even_in_trusted_run():
    # Anonymous takes precedence: an anonymous participant has no user row, so the
    # scope is the conversation id (Redis-only, never durable) regardless of the
    # authorization factors - the durable store's users FK would reject it.
    scope = _derive_memory_scope_id(
        is_anonymous_user=True,
        is_agent_level_writer=True,
        is_trusted_run=True,
        conversation_id="conv-1",
        user_id="",
    )
    assert scope == "conv-1"


# ── Payload Creator memory is disabled at the source ─────────────────────────


def _agent_by_slug(slug: str) -> dict:
    from app.agents.builtin_agents import BUILTIN_AGENTS

    for agent in BUILTIN_AGENTS:
        if agent["slug"] == slug:
            return agent
    raise AssertionError(f"built-in agent {slug!r} not found in BUILTIN_AGENTS")


def test_payload_creator_disables_every_memory_tier():
    pc = _agent_by_slug(_PC_SLUG)
    for flag in _MEMORY_FLAGS:
        assert pc.get(flag) is False, (
            f"Payload Creator must set {flag}=False (it runs under an operator id "
            "inside another agent's run; a durable write would pollute that "
            "operator's per-user memory)"
        )


def test_orchestrator_keeps_memory_enabled():
    # The orchestrator SHOULD learn (durable tradecraft across runs). It must NOT
    # disable persistent memory - it either omits the flag (inherits the model
    # default True) or sets it True.
    orch = _agent_by_slug(_ORCH_SLUG)
    assert orch.get("memory_persistent_enabled", True) is True


# ── WS5 end-to-end wiring (source-level pins) ────────────────────────────────
# The pure helper above is only half the contract. These guard the two seams
# that make an autonomous run - and ONLY an autonomous run - reach the
# agent-level branch, so a future refactor can't silently re-open the leak or
# drop WS5.


def test_run_agent_chat_gates_agent_level_on_two_factors_not_company_managed():
    # The call site must compute the agent-level gate from BOTH the
    # designated-writer identity (is_tradecraft_writer_agent) AND the trusted
    # autonomous run (in_autonomous_run_context), NOT from agent.is_company_managed
    # and NOT from the perimeter-gated in_tradecraft_run_context (which is False on
    # a first cycle whose scope has not resolved - the WS5 regression). Requiring
    # both is what keeps an ordinary company-managed chat AND a consulted
    # specialist (which inherits the run-context ContextVar) off the shared pool.
    import inspect

    from app.services import agent_chat as agent_chat_mod

    src = inspect.getsource(agent_chat_mod.run_agent_chat)
    assert "is_trusted_run = in_autonomous_run_context()" in src
    assert "is_agent_level_writer = is_tradecraft_writer_agent(" in src
    assert "is_agent_level_writer=is_agent_level_writer," in src
    assert "is_trusted_run=is_trusted_run," in src
    # The memory-scope run factor must NOT be the perimeter-gated write gate.
    assert "is_trusted_run = in_tradecraft_run_context()" not in src


def test_orchestrator_establishes_autonomous_run_context_unconditionally():
    # WS5 first-cycle fix: the memory-scope trusted-run signal must be set for the
    # WHOLE cycle, UNCONDITIONALLY - separate from the tradecraft-write context,
    # which is gated on the perimeter (a usable live scope OR a plan_mode
    # authoring run) and so is absent on a first LIVE cycle / outage. Pin that the
    # two contexts are established separately so a refactor can't re-collapse them
    # and re-break cycle 0's agent-level scope.
    import inspect

    from app.services import autonomous_pentest as ap_mod

    src = inspect.getsource(ap_mod.run_autonomous_cycle)
    # Whole-call autonomous-run marker, set unconditionally and reset in finally.
    assert "autonomous_run_token = set_autonomous_run_context()" in src
    assert "reset_autonomous_run_context(autonomous_run_token)" in src
    # The tradecraft WRITE context stays perimeter-gated and is a DIFFERENT seam.
    # RC-6(b): the gate is scope_view_is_usable(live_scope) OR plan_mode - a LIVE
    # run still requires a settled perimeter; a scenario-authoring run (no live
    # perimeter) is the only relaxation.
    assert "set_tradecraft_run_context(" in src
    assert "scope_view_is_usable(live_scope) or plan_mode" in src
    assert "run_agent_chat(" in src
    # The unconditional marker must not be the one hidden behind the scope guard.
    assert "set_autonomous_run_context()\n" in src
