"""Orchestrator arsenal: permissive gate, precedence-steering description.

Two halves that must NOT be conflated. (1) The GATE stays permissive - the
Attack Path Orchestrator keeps every authoring sub-action in hand. An earlier
sweep forbade create/duplicate "to stop orphans", which only made the model fail
differently and cost weeks of thrash; only ``bulk_delete`` is refused, because
its filter scope cannot be bounded to this run's creations. (2) The DESCRIPTION
steers a strict PRECEDENCE - reuse an installed contract first, FORK a
payload-based dead-end (the default, not an option), create an original ONLY to
fill a genuine gap. The operator complaint that drove this was "always creates,
never forks": the schema alone cannot fix that (nothing is removed from the
model's hand), so the prose has to. The clean-arsenal outcomes are still
guaranteed by mechanisms that never remove a tool (findings param + strict
normalizer + read-back verify, the step-before-more phase gate, the Semaphore(3)
write cap, and session-scoped delete so it cleans up its OWN mess).

These tests pin that contract:

* ``is_openaev_orchestrator_agent`` is true only for the built-in
  ``openaev-attack-path-orchestrator``.
* ``is_orchestrator_forbidden_arsenal_action`` is inert unless the run marker is
  on; then ONLY ``bulk_delete`` is refused (its filter scope cannot be bounded
  to this run's creations). create / duplicate / update / fork / delete / reads
  are all allowed.
* ``orchestrator_may_delete_action`` scopes ``delete`` to this run's OWN
  creations: unrestricted off-run, and on-run allowed only for an id recorded by
  create/fork/duplicate - never another session's / someone else's payload.
* Nested ``set_orchestrator_arsenal_writes(False)`` (the Payload Creator consult
  on the same asyncio task) lifts even the bulk_delete refusal, then reset
  restores the parent gate.
* ``narrow_orchestrator_arsenal_tools`` drops ONLY ``bulk_delete`` from the
  schema without mutating the original definition, and swaps in the
  precedence-steering description (reuse -> fork -> create).
* Source pins: ``run_agent_chat`` keys the gate on the current agent and
  ``threat_arsenal.py`` calls the execution-time checks.
"""

from __future__ import annotations

import inspect

import app.integrations.providers.openaev.threat_arsenal as threat_arsenal_mod
import app.services.agent_chat as agent_chat_mod
from app.services.builtin_tools import (
    is_openaev_orchestrator_agent,
    is_orchestrator_forbidden_arsenal_action,
    narrow_orchestrator_arsenal_tools,
    orchestrator_may_delete_action,
    record_orchestrator_session_action,
    reset_orchestrator_arsenal_writes,
    reset_orchestrator_session_actions,
    set_orchestrator_arsenal_writes,
    set_orchestrator_session_actions,
)
from app.tools.base import ToolDefinition

_FULL_ARSENAL_ENUM = [
    "list",
    "list_non_tabletop",
    "get",
    "create",
    "update",
    "fork",
    "duplicate",
    "delete",
    "bulk_delete",
    "domain_counts",
    "author_counts",
    "facet_counts",
    "argument_types",
    "security_platforms",
    "schemas",
]

# Every authoring path the orchestrator KEEPS - the point of the reversal. delete
# is here too (its use is bounded at runtime by orchestrator_may_delete_action,
# not by removing the sub-action).
_ORCHESTRATOR_ALLOWED_ACTIONS = (
    "list",
    "list_non_tabletop",
    "get",
    "create",
    "update",
    "fork",
    "duplicate",
    "delete",
    "domain_counts",
    "author_counts",
    "facet_counts",
    "argument_types",
    "security_platforms",
    "schemas",
)


def _arsenal_tool(enum: list[str] | None = None) -> ToolDefinition:
    return ToolDefinition(
        name="manage_openaev_threat_arsenal",
        description="fake tool manage_openaev_threat_arsenal",
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(enum if enum is not None else _FULL_ARSENAL_ENUM),
                }
            },
        },
        source="test",
    )


def _make_tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"fake tool {name}",
        input_schema={"type": "object", "properties": {}},
        source="test",
    )


def test_orchestrator_agent_is_only_the_builtin_slug():
    assert is_openaev_orchestrator_agent(
        "openaev-attack-path-orchestrator", is_builtin=True
    )
    assert not is_openaev_orchestrator_agent(
        "openaev-attack-path-orchestrator", is_builtin=False
    )
    assert not is_openaev_orchestrator_agent(
        "openaev-payload-creator", is_builtin=True
    )
    assert not is_openaev_orchestrator_agent(
        "openaev-scenario-creator", is_builtin=True
    )
    assert not is_openaev_orchestrator_agent(None, is_builtin=True)


def test_orchestrator_guard_is_inert_when_contextvar_off():
    # Off-run: chat / Payload Creator / tests keep the full contract - nothing is
    # ever refused, including bulk_delete.
    for action in ("create", "update", "duplicate", "delete", "bulk_delete"):
        assert not is_orchestrator_forbidden_arsenal_action(action), action


def test_orchestrator_guard_refuses_only_bulk_delete_when_on():
    # The reversal: with the run marker on, the orchestrator is FREE to author
    # (create/duplicate/update/fork) and to clean up its own creations (delete).
    # ONLY bulk_delete stays refused, because its filter scope cannot be bounded
    # to "what THIS run created" and the orchestrator must never delete another
    # session's / someone else's payloads.
    token = set_orchestrator_arsenal_writes(True)
    try:
        assert is_orchestrator_forbidden_arsenal_action("bulk_delete")
        for action in _ORCHESTRATOR_ALLOWED_ACTIONS:
            assert not is_orchestrator_forbidden_arsenal_action(action), action
    finally:
        reset_orchestrator_arsenal_writes(token)
    assert not is_orchestrator_forbidden_arsenal_action("bulk_delete")


def test_orchestrator_session_scoped_delete_allows_only_own_creations():
    # delete is not categorically forbidden - it is bounded to THIS run's own
    # creations. This is what lets the orchestrator purge its unused/bad copies
    # (Samuel: "delete unused, purge, rename") while never touching a payload
    # from another session or authored elsewhere ("of course, it should not
    # delete payloads created elsewhere or in other sessions").

    # Off-run: unrestricted (chat / other agents keep the full delete contract).
    assert orchestrator_may_delete_action("any-id") is True

    marker = set_orchestrator_arsenal_writes(True)
    ledger = set_orchestrator_session_actions(True)
    try:
        # Nothing authored yet -> every delete refused (empty ledger).
        assert orchestrator_may_delete_action("foreign-id") is False
        assert orchestrator_may_delete_action(None) is False
        # Record a creation this run -> that id becomes deletable; a payload from
        # another session / authored elsewhere never does.
        record_orchestrator_session_action("mine-1")
        assert orchestrator_may_delete_action("mine-1") is True
        assert orchestrator_may_delete_action("someone-elses") is False
    finally:
        reset_orchestrator_session_actions(ledger)
        reset_orchestrator_arsenal_writes(marker)
    # Reset -> back to unrestricted for the next (non-orchestrator) caller.
    assert orchestrator_may_delete_action("mine-1") is True


def test_record_session_action_is_a_no_op_off_run():
    # A delegated Payload Creator (marker False) authoring on the same task must
    # not populate the orchestrator's ledger.
    record_orchestrator_session_action("leaked")
    marker = set_orchestrator_arsenal_writes(True)
    ledger = set_orchestrator_session_actions(True)
    try:
        assert orchestrator_may_delete_action("leaked") is False
    finally:
        reset_orchestrator_session_actions(ledger)
        reset_orchestrator_arsenal_writes(marker)


def test_nested_payload_creator_lifts_bulk_delete_gate_then_parent_restores():
    # consult_agent awaits run_agent_chat on the same asyncio task, so the nested
    # call sets the marker False from the Payload Creator slug and the specialist
    # keeps the full, unrestricted contract - then reset restores the parent's
    # bulk_delete refusal.
    outer = set_orchestrator_arsenal_writes(True)
    try:
        assert is_orchestrator_forbidden_arsenal_action("bulk_delete")
        inner = set_orchestrator_arsenal_writes(False)
        try:
            assert not is_orchestrator_forbidden_arsenal_action("bulk_delete")
            assert not is_orchestrator_forbidden_arsenal_action("create")
            assert not is_orchestrator_forbidden_arsenal_action("duplicate")
        finally:
            reset_orchestrator_arsenal_writes(inner)
        assert is_orchestrator_forbidden_arsenal_action("bulk_delete")
    finally:
        reset_orchestrator_arsenal_writes(outer)


def test_narrow_orchestrator_arsenal_tools_drops_only_bulk_delete():
    other = _make_tool("search_openaev_injector_contracts")
    arsenal = _arsenal_tool()
    tools = [other, arsenal]

    kept = narrow_orchestrator_arsenal_tools(tools)
    assert [t.name for t in kept] == [
        "search_openaev_injector_contracts",
        "manage_openaev_threat_arsenal",
    ]
    enum = kept[1].input_schema["properties"]["action"]["enum"]
    # Only bulk_delete is stripped; every authoring path stays.
    assert "bulk_delete" not in enum
    for allowed in _ORCHESTRATOR_ALLOWED_ACTIONS:
        assert allowed in enum, allowed
    # Original definition is not mutated (Payload Creator / chat keep everything).
    assert "bulk_delete" in arsenal.input_schema["properties"]["action"]["enum"]
    assert other is kept[0]


def test_narrow_orchestrator_arsenal_tools_rewrites_shared_description():
    # The shared threat_arsenal description tells the Payload Creator to create /
    # update in place. The orchestrator copy must STEER a strict precedence -
    # reuse first, fork a payload-based dead-end (the default), create an original
    # only to fill a genuine gap - because the enum stays permissive and a
    # tool-calling model follows the prose. The old "outcome-driven freedom"
    # phrasing let it always create and never fork (the exact operator complaint).
    original_desc = (
        "REUSE OVER RE-CREATE: use action='update' with its action_id. "
        "'create' is only for genuinely new content."
    )
    arsenal = ToolDefinition(
        name="manage_openaev_threat_arsenal",
        description=original_desc,
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_FULL_ARSENAL_ENUM)},
            },
        },
        source="test",
    )
    kept = narrow_orchestrator_arsenal_tools([arsenal])
    desc = kept[0].description
    low = desc.lower()
    # A strict, ordered precedence - not a shrunk toolset and not "free to create".
    assert "precedence" in low
    assert "reuse" in low
    # Fork is the DEFAULT for a payload-based dead-end, not an optional path...
    assert "the default, not an option" in low
    # ...and an original create is the LAST resort, never the opening move.
    assert "last resort" in low
    assert "never the opening move" in low
    # The permissive "you are FREE / judged on the OUTCOME" framing is gone: it is
    # what produced "always creates, never forks".
    assert "free" not in low
    # Every authoring path is still present in the prose (the schema is unchanged;
    # only the steering is stricter).
    assert "fork" in low
    assert "duplicate" in low
    assert "create" in low
    assert "delete" in low  # session-scoped cleanup of its own creations
    # Only bulk_delete is called out as unavailable.
    assert "bulk_delete" in low
    # The findings param steers the model away from hand-building the nested
    # action_output_parsers it kept 400-ing on.
    assert "findings" in low
    # Delegation to the Payload Creator stays available for genuinely complex
    # originals (background task, not a synchronous consult).
    assert "delegate" in low
    assert "spawn_background_task" in desc
    assert "openaev-payload-creator" in desc
    assert "consult_agent" not in desc
    # The shared create-oriented prose is replaced, original left intact.
    assert "REUSE OVER RE-CREATE" not in desc
    assert "use action='update'" not in desc
    assert arsenal.description == original_desc


def test_shared_arsenal_description_forks_payload_based_dead_ends():
    """Payload Creator still sees the live tool description.

    The shared copy must teach the atomic ``fork`` op (one call: duplicate ->
    rename -> AI tag -> REGEX parsers -> read-back verify) and prefer it over the
    manual duplicate-then-update dance, without ever teaching update-in-place of
    a shared original.
    """
    src = inspect.getsource(threat_arsenal_mod)
    assert "'fork' it in " in src
    assert "Prefer 'fork' over the manual" in src
    assert "NEVER update a shared catalog" in src
    assert "REUSE OVER RE-CREATE" not in src


class TestOrchestratorArsenalWiring:
    RUN_AGENT_CHAT_SOURCE = inspect.getsource(agent_chat_mod.run_agent_chat)
    THREAT_ARSENAL_SOURCE = inspect.getsource(threat_arsenal_mod)

    def test_run_agent_chat_keys_gate_on_current_agent(self):
        src = self.RUN_AGENT_CHAT_SOURCE
        assert "is_openaev_orchestrator_agent" in src
        assert "narrow_orchestrator_arsenal_tools" in src
        assert "set_orchestrator_arsenal_writes" in src
        assert "reset_orchestrator_arsenal_writes" in src
        # The session-scoped cleanup ledger shares the same run lifecycle.
        assert "set_orchestrator_session_actions" in src
        assert "reset_orchestrator_session_actions" in src

    def test_threat_arsenal_calls_orchestrator_checks(self):
        src = self.THREAT_ARSENAL_SOURCE
        # bulk_delete refusal, session-scoped delete gate, and the ledger writes.
        assert "is_orchestrator_forbidden_arsenal_action" in src
        assert "orchestrator_may_delete_action" in src
        assert "record_orchestrator_session_action" in src
        # Findings authoring stays taught in code.
        assert "REGEX" in src
        assert "findings" in src

    def test_fork_reuse_never_records_a_prior_run_id_into_session_ledger(self):
        # Regression: the session-scoped delete ledger
        # must hold ONLY ids THIS run actually minted, so the orchestrator can
        # purge its OWN unused copies but never another session's payload.
        # ``_finalize_fork`` is shared by the fresh-mint path AND the cross-cycle
        # REUSE path, whose ``target_id`` can be a fork minted by a PRIOR run that
        # ``_find_existing_fork`` rediscovered via the source-keyed provenance
        # marker. Recording inside ``_finalize_fork`` would let a reuse-then-delete
        # purge that prior-run copy, so the record MUST live at the fresh mint
        # (right after the ``/duplicate``) and NEVER inside ``_finalize_fork``.
        src = self.THREAT_ARSENAL_SOURCE
        start = src.index("async def _finalize_fork(")
        end = src.index("def _scoped_search_body(", start)
        finalize_src = src[start:end]
        assert "record_orchestrator_session_action" not in finalize_src, (
            "the session-ledger record leaked back into the shared _finalize_fork; "
            "a reused prior-run fork would become deletable by this run"
        )
        # The fresh-duplicate mint path is where the new copy id is recorded.
        assert "record_orchestrator_session_action(new_id)" in src

    def test_duplicate_is_bound_by_the_same_outcome_controls_as_fork(self):
        # Regression: the orchestrator may now `duplicate`,
        # but a raw duplicate MINTS a copy, so it must be capped, phase-gated and
        # counted exactly like fork/create - otherwise a parallel burst of raw
        # duplicates bypasses every outcome control and reopens the orphan pattern
        # the PR exists to prevent.
        src = self.THREAT_ARSENAL_SOURCE
        # Both the write cap and the step-before-more phase gate include duplicate
        # in their action tuple.
        assert src.count('("fork", "create", "duplicate")') >= 2
        # The duplicate branch mints its copy under the write guard and consumes
        # the phase-gate lead via the authored counter (like fork/create).
        start = src.index('if action == "duplicate":')
        end = src.index('if action == "delete":', start)
        duplicate_src = src[start:end]
        assert "_arsenal_write_guard(action)" in duplicate_src
        assert "record_orchestrator_arsenal_authored()" in duplicate_src
        # The mint is accounted for BEFORE the best-effort tagging awaits, so a
        # cancellation after the POST but during tagging still leaves the copy
        # counted and cleanable (session-scoped delete), not an untracked orphan.
        rec = duplicate_src.index("record_orchestrator_session_action")
        tag = duplicate_src.index("_ensure_ai_tag_id")
        assert rec < tag, "ledger record must precede the tagging awaits"
