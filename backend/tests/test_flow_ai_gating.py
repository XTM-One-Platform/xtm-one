"""Per-flow AI gating: "AI off" must not mean "automation off" (US5).

The asymmetry that shapes all of this (FR-030): wrongly holding a flow back
produces a visible, reversible skip with a stated reason; wrongly running one
invokes a model on a platform whose entire purpose is that none is invoked.
Everything unclear therefore resolves to "requires AI".
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.services.flow_catalog import (
    NODE_KINDS,
    NODE_KINDS_BY_ID,
    NodeKind,
    graph_ai_dependency,
)


def _graph(*kinds: str) -> dict:
    return {
        "nodes": [
            {"id": f"n{i}", "kind": kind, "config": {}}
            for i, kind in enumerate(kinds)
        ]
    }


# --- T044: the default is the safe one ------------------------------------


def test_a_new_node_kind_defaults_to_requiring_ai():
    """The whole safety property of FR-015/FR-030 in one assertion.

    A node type added by someone who never heard of this feature must be held
    back, not run. This is the opposite default to every other flag on
    ``NodeKind``, so it is the one most likely to be "tidied" to match its
    neighbours.
    """
    fresh = NodeKind(
        kind="action.invented_later",
        category="actions",
        label="Invented later",
        description="A node type added without thinking about AI at all.",
        icon="Sparkles",
    )
    assert fresh.requires_ai is True


def test_an_unknown_kind_in_a_graph_requires_ai():
    """A graph naming a node type this build does not have is either newer than
    this build or corrupt. Neither is a reason to assume no model is involved.
    """
    needs_ai, called = graph_ai_dependency(_graph("action.from_the_future"))
    assert needs_ai is True
    assert called == set()


def test_every_catalog_kind_is_classified_explicitly():
    """Every exemption is one reviewable line, so none is an oversight.

    Asserted as a snapshot: adding an AI node kind, or exempting one, has to
    change this list deliberately. The count is the point — a silent flip of
    one entry is what this catches.
    """
    ai = {nk.kind for nk in NODE_KINDS if nk.requires_ai}
    assert ai == {
        "trigger.chat",
        "agent",
        "capability.tool",
        "capability.integration",
        "capability.skill",
        "capability.knowledge_base",
        "capability.structured_output",
        "capability.memory",
        "capability.guardrails",
        "capability.llm",
        "capability.on_delegate",
        "capability.shared_state",
        "capability.work_items",
        "capability.workset",
        "capability.budget",
        "capability.workspace",
        # A decision model (CLM) call.
        "condition.classify",
        "action.trigger_assignment",
        "action.call_tool",
        "action.workset_search",
        "action.workset_add",
        # Chunked and embedded: an embedding provider runs.
        "action.workspace_add_source",
        # The voice-over is synthesised by a speech model.
        "action.render_video",
        # Its report is written by a model.
        "action.deep_investigation",
    }, "the AI classification changed — was that deliberate?"


def test_the_workset_nodes_are_classified_from_their_executors():
    """Two of them read as deterministic and are not.

    ``workset_search`` calls ``hybrid_search_embeddings``; ``workset_add``
    returns ``pending_documents`` that ``_flush_workset_queue`` hands to
    ``enqueue_document_processing``, i.e. embedding jobs. Both were readable as
    deterministic from their node descriptions alone — ``add`` was classified
    wrongly on exactly that basis. ``seal`` only reads existing embeddings.
    """
    assert NODE_KINDS_BY_ID["action.workset_search"].requires_ai is True
    assert NODE_KINDS_BY_ID["action.workset_add"].requires_ai is True
    assert NODE_KINDS_BY_ID["action.workset_seal"].requires_ai is False


def test_the_ambiguous_three_require_ai():
    """FR-030 names them: a tool call with model-chosen arguments,
    guardrails, structured output. Called out separately from the snapshot
    above because these are the ones someone will argue about."""
    for kind in (
        "action.call_tool",
        "capability.guardrails",
        "capability.structured_output",
    ):
        assert NODE_KINDS_BY_ID[kind].requires_ai is True, kind


def test_every_capability_requires_ai():
    """A capability exists to be used by a model and never executes standalone,
    so the family is one rule rather than a dozen judgement calls."""
    for nk in NODE_KINDS:
        if nk.kind.startswith("capability."):
            assert nk.requires_ai is True, nk.kind


# --- T043: deterministic graphs are let through ---------------------------


def test_a_deterministic_graph_does_not_require_ai():
    """The point of the feature: schedule → HTTP → notify keeps running."""
    needs_ai, called = graph_ai_dependency(
        _graph("trigger.schedule", "action.http_request", "action.notify")
    )
    assert needs_ai is False
    assert called == set()


def test_one_ai_node_is_enough_to_hold_the_graph_back():
    graph = _graph("trigger.schedule", "action.http_request", "agent")
    assert graph_ai_dependency(graph)[0] is True


def test_an_annotation_mentioning_ai_does_not_hold_a_graph_back():
    """Notes are not part of the executable graph, so a sticky note must not
    stop a deterministic flow. ``util.note`` is exempt anyway — this pins the
    annotation skip itself, which is what protects a *future* AI-ish
    annotation kind."""
    assert NODE_KINDS_BY_ID["util.note"].is_annotation is True
    graph = {
        "nodes": [
            {"id": "n0", "kind": "trigger.schedule", "config": {}},
            # Same shape an unknown-but-annotated kind would have.
            {"id": "n1", "kind": "util.note", "config": {}},
        ]
    }
    assert graph_ai_dependency(graph)[0] is False


def test_malformed_nodes_are_ignored_rather_than_crashing_the_sweep():
    """The resolver runs on every scheduler tick for every active flow; one
    corrupt node must not take the sweep down."""
    graph = {"nodes": ["not-a-dict", None, {"id": "n0", "kind": "action.notify"}]}
    assert graph_ai_dependency(graph)[0] is False


# --- flow.call is answered, not guessed ----------------------------------


def test_flow_call_is_reported_as_a_callee_not_as_an_ai_node():
    """Neither blanket answer is right: marking ``flow.call`` AI stops every
    deterministic composition, marking it safe lets AI through it. The node
    names its callee, so the caller resolves it instead."""
    target = str(uuid.uuid4())
    graph = {
        "nodes": [
            {"id": "n0", "kind": "trigger.schedule", "config": {}},
            {"id": "n1", "kind": "flow.call", "config": {"flow_id": target}},
        ]
    }
    needs_ai, called = graph_ai_dependency(graph)
    assert needs_ai is False
    assert called == {target}


def test_a_flow_call_with_no_target_adds_no_dependency():
    """An unset target executes nothing, so it cannot need a model."""
    graph = {
        "nodes": [{"id": "n0", "kind": "flow.call", "config": {"flow_id": ""}}]
    }
    needs_ai, called = graph_ai_dependency(graph)
    assert needs_ai is False
    assert called == set()


# --- The dispatch paths -----------------------------------------------------


def test_every_automatic_dispatch_path_asks_the_question():
    """An allowlist of dispatch sites, not a count of gates.

    Each ``create_flow_run`` in the trigger paths is a way a flow starts
    without anyone clicking anything, so each needs the gate above it. A sixth
    trigger kind added later fails here rather than quietly dispatching AI.
    """
    from app.services import flow_events, flow_triggers

    triggers_src = inspect.getsource(flow_triggers)
    # Five automatic paths: schedule, webhook, integration event, work item,
    # platform event.
    assert triggers_src.count("hold_back_if_ai_required(") == 6, (
        "5 dispatch sites + the definition — a new trigger path must gate too"
    )
    assert "hold_back_if_ai_required" in inspect.getsource(flow_events)


def test_the_held_back_run_creates_no_orchestration_execution():
    """What actually guarantees "zero steps executed" (SC-005).

    Not a polite decline inside the walker — the walker never sees the run,
    because no execution row is created for it. A future edit that routes this
    through ``create_flow_run`` would start the flow and then fail part-way,
    which is the state FR-016 exists to prevent.
    """
    import ast
    import textwrap

    from app.services import flow_triggers

    # Parsed, not grepped: the comments in this function legitimately *mention*
    # ``create_flow_run`` (it is the thing whose sharing snapshot they mirror),
    # and a substring assertion failed on that mention.
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(flow_triggers.hold_back_if_ai_required))
    )
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name | ast.Attribute)
    }
    assert "create_flow_run" not in called
    assert "notify_orchestration_wake" not in called

    source = inspect.getsource(flow_triggers.hold_back_if_ai_required)
    assert "FLOW_RUN_STATUS_SKIPPED" in source


def test_the_reason_is_written_where_runs_are_reviewed():
    """FR-016 wants a reason a user can read in Operations, not only a log
    line, so it goes on the run row itself."""
    from app.services import flow_triggers

    source = inspect.getsource(flow_triggers.hold_back_if_ai_required)
    assert "result_summary=" in source
    assert "AI is disabled" in flow_triggers._AI_HELD_BACK_REASON
    assert "No step was executed" in flow_triggers._AI_HELD_BACK_REASON


def test_skipped_is_a_real_run_status_and_not_an_active_one():
    """A held-back run must not look like work in progress: the overlap guard
    (``has_active_run``) would then block the flow's next real trigger."""
    from app.models.flow_run import (
        FLOW_RUN_ACTIVE_STATUSES,
        FLOW_RUN_STATUS_SKIPPED,
        FLOW_RUN_STATUSES,
    )

    assert FLOW_RUN_STATUS_SKIPPED in FLOW_RUN_STATUSES
    assert FLOW_RUN_STATUS_SKIPPED not in FLOW_RUN_ACTIVE_STATUSES


def test_the_check_constraint_admits_skipped():
    """The model and the migration have to agree, or every insert 500s."""
    from pathlib import Path

    from app.models.flow_run import FlowRun

    constraint = next(
        c for c in FlowRun.__table__.constraints
        if getattr(c, "name", "") == "ck_flow_runs_status"
    )
    assert "skipped" in str(constraint.sqltext)

    migration = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "ai2skipped02_add_flow_run_skipped_status.py"
    ).read_text(encoding="utf-8")
    assert "'skipped'" in migration


@pytest.mark.asyncio
async def test_a_deterministic_flow_is_not_held_back_when_ai_is_off(monkeypatch):
    """The behavioural half of SC-005's first clause, without a database.

    ``flow_requires_ai`` must clear a deterministic graph before anything else
    happens, so the caller dispatches normally.
    """
    from app.services import flow_triggers

    class _Flow:
        id = uuid.uuid4()
        name = "deterministic"
        graph = _graph("trigger.schedule", "action.http_request", "action.notify")

    # db is never touched: no flow.call means no lookup.
    assert await flow_triggers.flow_requires_ai(None, _Flow()) is False


@pytest.mark.asyncio
async def test_an_ai_flow_is_held_back_without_touching_the_database(monkeypatch):
    from app.services import flow_triggers

    class _Flow:
        id = uuid.uuid4()
        name = "ai-bearing"
        graph = _graph("trigger.schedule", "agent")

    assert await flow_triggers.flow_requires_ai(None, _Flow()) is True


@pytest.mark.asyncio
async def test_ai_is_checked_before_the_graph_so_a_normal_platform_pays_nothing(
    monkeypatch,
):
    """On an ordinary platform this gate must cost one cached boolean, not a
    graph walk per flow per tick. "Ordinary" is AI on AND licensed:
    the background gate's answer, both halves pinned open."""
    from app.api.v1 import platform as platform_api
    from app.services import ai_state as ai_state_mod
    from app.services import flow_triggers

    walked = []

    async def _enabled(_db):
        return True

    def _spy(graph):
        walked.append(graph)
        return False, set()

    monkeypatch.setattr(ai_state_mod, "is_ai_enabled", _enabled)
    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _enabled)
    monkeypatch.setattr("app.services.flow_catalog.graph_ai_dependency", _spy)

    class _Flow:
        id = uuid.uuid4()
        name = "any"
        graph = _graph("agent")

    assert await flow_triggers.hold_back_if_ai_required(
        None, _Flow(), trigger_type="schedule"
    ) is False
    assert walked == [], "the graph must not be walked when AI is on"


# --- The deterministic surfaces stay open (FR-014) ------------------------


def test_deterministic_flow_surfaces_are_not_behind_the_ai_state_gate():
    """"AI off" must not mean "flows off" (FR-014, UPGRADING.md).

    ``require_agentic_ai`` refuses on the *resolved AI state*, so leaving these
    endpoints behind it meant a schedule→HTTP→notify flow could not be created,
    activated or run on an AI-disabled platform, and an operator could not even
    cancel a run already in flight. It also contradicted the flow-builder banner
    this feature adds ("Editing it still works").

    They take the licence question instead, and whether a *particular* flow may
    run is answered per flow.
    """
    import inspect

    from app.api.v1 import flow_event_types, flow_runs, flows, integrations, variables

    for module in (flows, flow_runs, flow_event_types, variables, integrations):
        source = inspect.getsource(module)
        # BOTH names: ``require_ai_enabled`` refuses on the resolved AI state
        # just as ``require_agentic_ai`` does. Checking only the latter is why
        # ``activate_flow`` and ``deactivate_flow`` sat behind the AI gate for a
        # whole review cycle while this test stayed green (found by
        # ``/code-review``, 2026-08-28). Deactivate was the worse of the two:
        # it only ever *reduces* AI work, and blocking it left an operator
        # unable to stop an AI-bearing flow writing a skipped run every tick.
        offenders = [
            line.strip()
            for line in source.splitlines()
            if "Depends(require_agentic_ai)" in line
            or "Depends(require_ai_enabled)" in line
        ]
        assert not offenders, (
            f"{module.__name__} gates a deterministic-capable surface on the "
            f"resolved AI state: {offenders}"
        )


def test_a_manual_run_is_refused_per_flow_not_per_platform():
    """The manual counterpart of the trigger gate.

    Refused rather than recorded as skipped: someone clicked Run and deserves
    the answer. The refusal carries the contract code so a client can branch.
    """
    import inspect

    from app.api.v1.flow_runs import run_flow

    source = inspect.getsource(run_flow)
    assert "flow_requires_ai" in source
    assert "refuse_ai_disabled" in source


def test_the_hold_back_record_is_debounced():
    """One record per held-back stretch, not one per tick.

    The work-item sweep reaches the gate on every autonomous tick whether or
    not there is work, so an unconditional insert grew ``flow_runs`` without
    bound and buried the signal the status exists to give.
    """
    import inspect

    from app.services import flow_triggers

    source = inspect.getsource(flow_triggers.hold_back_if_ai_required)
    assert "FLOW_RUN_STATUS_SKIPPED" in source
    # Per trigger *kind*, not per flow. Comparing only "newest run of any kind"
    # meant a scheduled skip swallowed a later webhook delivery: the caller got
    # held-back with nothing recorded, so a partner's POST vanished with no trace
    # in Operations (/code-review, 2026-08-28). A discrete external delivery
    # earns its own record; a tick that repeats still does not.
    assert "FlowRun.trigger_type == trigger_type" in source
    # And "nothing real has happened since", or the two checks leapfrog: a flow
    # with both a work-item tick and a webhook alternated trigger types, each
    # seeing a mismatch, and inserted a row per tick *and* per delivery
    # (/code-review, 2026-08-28).
    assert "real_run_since" in source


def test_the_hold_back_record_uses_its_own_session():
    """``emit_flow_event`` calls the gate with the walker's session mid-node.

    Committing the caller's session there would publish the walker's in-flight
    burst early and remove its ability to roll the step back.
    """
    import inspect

    from app.services import flow_triggers

    source = inspect.getsource(flow_triggers.hold_back_if_ai_required)
    assert "async_session() as own" in source
    assert "await db.commit()" not in source


def test_the_held_back_run_carries_every_sharing_tier():
    """A skipped run must be as invisible as the flow it belongs to.

    ``get_visible_run`` re-implements the flow's visibility filter over the
    run's own columns, so a tier missing from the snapshot is a tier the run
    journal fails to enforce — ``FlowRun``'s own comment says so.
    Setting only ``is_company_managed`` made an ``access_restricted`` flow's
    held-back run readable by every authenticated user, frozen graph included,
    and hid a group-shared flow's run from its legitimate viewers.
    """
    from app.services import flow_triggers

    source = inspect.getsource(flow_triggers.hold_back_if_ai_required)
    for column in (
        "is_company_managed=",
        "shared_group_ids=",
        "shared_user_ids=",
        "access_restricted=",
    ):
        assert column in source, column
