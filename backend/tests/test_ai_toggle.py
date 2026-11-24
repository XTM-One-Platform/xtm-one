"""The toggle's structural guarantees (US2).

Three things the integration tests cannot see, because calling the endpoint
function bypasses the dependency and because a passing test proves nothing
about the code that was *not* written: the administrator check, the tri-state
on the serializer, and the single wire polarity.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.api.deps import require_admin
from app.api.v1 import settings as settings_api
from app.services import ai_state

# --- FR-024: administrator authorisation ----------------------------------


def test_the_write_path_requires_an_administrator():
    """Reused rather than reinvented: the toggle rides on the existing platform
    settings route, so it inherits that route's check. If someone relaxes the
    route to ``get_current_user``, this fails."""
    params = inspect.signature(
        settings_api.update_platform_settings_endpoint
    ).parameters
    assert params["user"].default.dependency is require_admin


# --- The tri-state survives the round trip --------------------------------


@pytest.mark.parametrize(
    ("stored", "wire"), [(None, None), (True, False), (False, True)]
)
def test_the_column_and_the_wire_round_trip(stored, wire):
    """``None`` must stay ``None`` in both directions. Collapsing it to
    ``False`` pins the platform to disabled and hides every later change to the
    deployment default (``contracts/ai-state-api.md``)."""
    assert ai_state.override_to_wire(stored) is wire
    assert ai_state.override_from_wire(wire) is stored


def test_the_serializer_returns_the_override_not_the_resolved_state(monkeypatch):
    """The form needs to know whether it is looking at an explicit choice or at
    the deployment default, or it cannot decide whether to offer the reset."""

    class _PS:
        id = "00000000-0000-0000-0000-000000000000"
        ai_disabled = None

        def __getattr__(self, name):
            return None

    monkeypatch.setattr(ai_state.settings, "platform_mode", "ai_disabled")
    body = settings_api._serialize_platform_settings(_PS(), ai_state.settings)

    assert body["ai_enabled"] is None, "an unset override must not read as false"
    assert body["ai_enabled_default"] is False, "which is what a reset resolves to"


# --- One polarity on the wire ---------------------------------------------


def test_no_api_module_names_the_negative_polarity_on_the_wire():
    """The contract's one hard rule. An earlier draft had clients reading
    ``ai_enabled`` and writing ``ai_disabled`` — two names differing only by a
    negation, at the boundary where getting it backwards runs models on a
    deployment that forbids them.

    Two rules, both checked on the syntax rather than the text so that
    ``refuse_ai_disabled`` and ``AI_DISABLED_CODE`` do not read as violations:
    no route may use ``ai_disabled`` as a field name, and no route may write the
    column — the inversion belongs to ``services/ai_state.py``.
    """
    api = Path(settings_api.__file__).parent.parent
    named, written = [], []
    for path in api.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "ai_disabled" not in source:
            continue
        where = str(path.relative_to(api.parent))
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and node.value == "ai_disabled":
                named.append(f"{where}:{node.lineno}")
            targets = (
                node.targets if isinstance(node, ast.Assign)
                else [node.target] if isinstance(node, ast.AugAssign | ast.AnnAssign)
                else []
            )
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "ai_disabled":
                    written.append(f"{where}:{node.lineno}")

    assert not named, f"``ai_disabled`` used as a wire field name in: {named}"
    assert not written, f"the column written outside ai_state.py in: {written}"


# --- FR-013: work in flight is left alone ---------------------------------


def test_the_ai_gate_is_read_at_entry_only_never_mid_execution():
    """Turning AI off stops *new* dispatch. A run already executing finishes
    under its own timeout, and it does so because nothing inside the execution
    path asks whether AI is still on — not because something politely declines
    to cancel it.

    An allowlist rather than a count: a fifth caller is fine if it is another
    entry point, and a defect if it is a step inside a run, where a mid-flight
    refusal would leave the half-written run and traces this mode exists to
    avoid. Adding one here is the moment to decide which it is.
    """
    entry_points = {
        "api/deps.py",  # the request gate, before any work starts
        "api/v1/platform.py",  # reporting the state to clients
        "api/v1/settings.py",  # the toggle itself
        # The manual-run endpoint, asked per flow before the run exists. An
        # interactive caller is refused outright rather than handed a skip
        # record — someone clicked Run and deserves the answer.
        "api/v1/flow_runs.py",
        # The MCP surfaces, refused entirely with AI off (FR-029). All three ask
        # after authentication and before any work, so the endpoint states why
        # it refuses instead of disappearing.
        "api/v1/mcp_endpoint.py",
        "api/v1/mcp_platform.py",
        "api/v1/mcp_xtm.py",
        # The OpenAI-compatible and Anthropic proxies (Cursor, Claude Code):
        # asked after the API key is authenticated and before any provider
        # call, with the same two-reason refusal as the MCP surfaces - AI off
        # answers the contract code, an absent licence the chat sentence.
        "api/v1/llm_proxy.py",
        # The assignment service, gated once so the tick, the manual route and
        # the external webhooks all inherit it — before any run row exists.
        "services/assignment_engine.py",
        # The A2A task surface: another agent invoking ours, refused before the
        # body is parsed (FR-010).
        "api/v1/a2a_server.py",
        # The worker: refused at job entry, so a job queued before the toggle
        # does not embed. Nothing in flight is cancelled — the check happens
        # before the job's first await that does work (FR-007, FR-013).
        "worker.py",
        # Not listed, deliberately: an ``action.trigger_assignment`` node and the
        # delegate-to-assignment tool both start a NEW unit of AI work from
        # inside a run, and both surfaced ``trigger_assignment``'s
        # ``HTTPException`` as an opaque 403 (/code-review, 2026-08-28). They now
        # **catch** that refusal and answer in their own terms rather than asking
        # the gate, so nothing inside an execution path reads the AI state and
        # this test keeps its meaning.
        # Every external channel: Slack, Teams, Telegram, Discord, WhatsApp
        # and Google Chat all funnel through ``handle_message``, which is
        # asked before the conversation or the inbound message is stored, so
        # a refused message leaves no half-written state (FR-010). An entry
        # point, not a step inside a run: gating each adapter instead is how
        # all six were missed at once.
        "channels/router.py",
        # The public widget's session endpoint, asked before a session exists.
        # Inline rather than via a dependency because this module takes no
        # ``Depends(get_db)`` and opens its own sessions.
        "api/v1/public_chat.py",
        # Two PUT handlers, gated at the top before any work starts. Here to
        # tell "AI is off" from "the licence lacks the add-on" — one refusal
        # carries the contract code, the other keeps the licence message.
        "api/v1/ctem_metrics.py",
        # The Video Studio editor's preview, asked before the preview is built:
        # whether a manager's draft has its missing narrations spoken. The
        # preview itself still answers with AI off.
        "api/v1/video_studio.py",
        "services/autonomous.py",  # once per scheduler tick, before dispatch
        # Per-flow, at trigger time, before the run exists (US5). An
        # entry point in the strict sense: the walker in flow_runner.py still
        # asks nothing, which is what lets a running flow finish.
        "services/flow_triggers.py",
        "services/provisioning.py",  # which content set to create
        "main.py",  # boot
        # Not a gate at all: telemetry *reports* the resolved state (FR-031) and
        # gates nothing. Listed because this test asks "does anything read the
        # AI state mid-run", and an observer is a legitimate fourth answer
        # alongside request, tick and boot.
        "services/telemetry.py",
        # The background gate: the AI state AND the licensed edition,
        # one answer for the work nobody waits on. Its own readers are
        # allow-listed below, by the same rule.
        "services/agentic_gate.py",
        # Platform Health agent checks (#101 S3): asked once when a check starts
        # its read, before any row is read; agents do not run with AI off, so
        # the checks read nothing then. No model call, no run in flight.
        "services/health/agent_checks.py",
    }
    # Who asks the background gate, and where: always before the work starts.
    # A run already executing never asks, which is what lets it finish (FR-013)
    # after AI is switched off or the deployment drops to Community Edition.
    combined_gate_entry_points = {
        # Before the conversation or the inbound message is stored.
        "channels/router.py",
        # ``trigger_assignment`` before any run row exists; a run's start
        # before its quota is charged (a queued run has not started - new
        # work, not work in flight); the chained next assignment once a run
        # has finished; the delivery queue before it promotes one.
        "services/assignment_engine.py",
        # Once per scheduler tick, and the per-user fan-out it dispatches.
        "services/autonomous.py",
        # Per polled assignment, before anything is read or claimed from the
        # provider (a cached read).
        "services/event_poller.py",
        # Per flow, at trigger time, before the run exists.
        "services/flow_triggers.py",
        # A delegated task's start, before its agent turn.
        "services/sub_agent.py",
        # A wake turn, before the wake message is written.
        "services/parent_wake.py",
        # The per-worker sweep of turns cut by a platform stop, before it
        # takes a queued record and starts its resume; it replaces
        # the scheduler-tick sweep that asked through autonomous.py.
        "services/cut_turn_resume.py",
        # A fresh evaluation, before its first run; an episode resume is in
        # flight and never asks.
        "services/orchestration_adapters/evaluation.py",
        # A held execution's re-claim, before its body starts: the hold is
        # closed once agentic AI may run again; never asked for an
        # execution that was not held.
        "services/ai_hold.py",
        # Job entry, as for the AI state above.
        "worker.py",
        # An agent account's message, once the rules decided to answer and
        # before the grace period or the agent turn starts.
        "services/agent_accounts/pipeline.py",
        # A scheduled work item's run start, before its action is sent or its
        # agent turn starts; a run that started never asks again.
        "services/scheduled_work_item_runner.py",
        # The work-item inbox pass a dispatch wake runs, the twin of the
        # scheduler tick's: asked once before the pass dispatches any item.
        "services/work_item_dispatch_wake.py",
        # Platform Health's budget (#8254): ``open_cycle`` and
        # ``budget_refusal`` ask before a cycle starts any work. ``reserve`` and
        # ``remaining``, read by a run in flight, check Platform Health's own caps
        # only.
        "services/health/budget.py",
        # A new fleet's first documentation (#10316): asked once at creation,
        # before the background generation is scheduled; never mid-generation.
        "services/fleet_doc_on_create.py",
        # The once-per-build rewrite of unreadable decision cards (#776): asked
        # once at the build's first boot, before any item is read or model called.
        "services/work_item_ask_backfill.py",
    }
    root = Path(ai_state.__file__).parent.parent
    callers = set()
    combined_callers = set()
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "agentic_gate" in source and path.name != "agentic_gate.py":
            # The resolvers only. Importing ``refusal_code`` or the codes is
            # recognising a refusal already caught, not reading the state -
            # the same distinction drawn for ``AI_DISABLED_CODE`` below.
            for node in ast.walk(ast.parse(source)):
                if (
                    isinstance(node, ast.ImportFrom)
                    and "agentic_gate" in (node.module or "")
                    and any(
                        a.name in {"agentic_refusal", "agentic_verdict", "is_licensed"}
                        for a in node.names
                    )
                ):
                    combined_callers.add(path.relative_to(root).as_posix())
        if path.name == "ai_state.py" or "ai_state" not in source:
            continue
        # Imports, not mentions: two modules carry the name in a comment
        # explaining the column, which is documentation, not a caller.
        #
        # And only imports of a **state resolver**. Importing ``AI_DISABLED_CODE``
        # is the opposite of reading the state: it is what a module does to
        # recognise the refusal it just *caught*, which is how a flow node and an
        # agent tool answer without asking the gate (/code-review, 2026-08-28).
        # Counting any ``ai_state`` import made those two look like mid-execution
        # readers, which is precisely what this test exists to forbid — so it
        # would have flagged the fix for the problem it guards against.
        resolvers = {"is_ai_enabled", "is_ai_enabled_strict", "ai_enabled_for_display"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                if "ai_state" not in (node.module or ""):
                    continue
                if any(a.name in resolvers for a in node.names):
                    callers.add(path.relative_to(root).as_posix())
            elif isinstance(node, ast.Import):
                # ``import app.services.ai_state`` — the symbol used is not
                # visible here, so count it and let the allowlist decide.
                if any("ai_state" in a.name for a in node.names):
                    callers.add(path.relative_to(root).as_posix())

    assert callers <= entry_points, (
        f"the AI gate reached beyond the entry points: {sorted(callers - entry_points)}"
    )
    assert combined_callers <= combined_gate_entry_points, (
        "the background gate reached beyond the entry points: "
        f"{sorted(combined_callers - combined_gate_entry_points)}"
    )


# --- The toggle must stop *every* chat entry point -------------------------


@pytest.mark.asyncio
async def test_disabling_ai_closes_chat_for_the_callers_that_bypass_the_dependency(
    monkeypatch,
):
    """``require_chat_enabled`` is not the only gate.

    Seven callers ask ``is_chat_enabled`` directly instead of going through the
    dependency — the chat WebSocket, the unauthenticated public-chat widget,
    the MCP endpoint and the OpenAI-compatible LLM proxy. With the check only
    on the dependency, an administrator turning AI off closed the UI while the
    proxy and the embed widget kept serving model traffic.

    ``db`` is a sentinel with no database behaviour: if the short-circuit ever
    moves below the tier lookup, this raises instead of silently passing.
    """
    from app.api.v1 import platform as platform_api
    from app.services import ai_state as ai_state_mod

    async def _disabled(_db):
        return False

    monkeypatch.setattr(ai_state_mod, "is_ai_enabled", _disabled)

    assert await platform_api.is_chat_enabled(object()) is False


def test_every_chat_gate_asks_the_ai_question_one_way_or_the_other():
    """Pins the fan-in the fix relies on.

    These callers share no dependency, so the AI check lives inside
    ``is_chat_enabled`` and they inherit it. ``mcp_endpoint`` and ``llm_proxy``
    are the exceptions and ask explicitly (``is_ai_enabled`` +
    ``is_chat_licensed``) because FR-029 wants them to state *which* reason
    they are refusing for — AI off, or an absent licence — rather than collapse
    both into the licence message: their clients (MCP hosts, Cursor, Claude
    Code) display the text as is.

    Asserted as a set so a new chat entry point has to be classified into one
    of the two shapes rather than quietly resolving the tier itself.
    """
    inherits: set[str] = set()
    asks_directly: set[str] = set()
    for f in (Path(__file__).resolve().parent.parent / "app").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        if "def is_chat_enabled" in text or "def is_chat_licensed" in text:
            continue
        # Not exclusive: ``deps.py`` holds both dependencies.
        if "is_chat_enabled(" in text:
            inherits.add(f.name)
        if "is_chat_licensed(" in text:
            asks_directly.add(f.name)

    assert inherits == {
        "deps.py",  # require_chat_enabled — the dependency
        "public_chat.py",  # the unauthenticated embed widget
        # A2A is offered exactly while agentic chat is. Its routes take
        # ``require_chat_enabled``; these are the paths with no request to
        # hang a dependency on — the sweep, the pre-flight, the tool wiring
        # and the connectivity probes. ``integrations.py`` is absent by
        # design: its A2A routes take the dependency, and its catalog
        # declares the gate rather than resolving it.
        "sweep.py",
        "delegation.py",
        # The callback receiver: a route, but one whose refusal must be
        # indistinguishable from its every other refusal, so it resolves the
        # gate itself rather than taking the dependency and answering 403.
        "a2a_callbacks.py",
        "integration_tools.py",
        "probe.py",
        "entra_a2a.py",
        "google_a2a.py",
        "oauth_a2a.py",
        # Not an entry point: the Security Audit asks whether a General
        # Assistant turn can run at all (its admin-reach finding).
        "security_audit.py",
        # Not an entry point either: whether the General Assistant can ask
        # before running tools, for the approval screens.
        "tool_approval_usage.py",
    }, inherits
    assert asks_directly == {
        "mcp_endpoint.py",  # refuses with the reason, per FR-029
        "llm_proxy.py",  # the OpenAI-compatible / Anthropic proxies, same shape
    }, asks_directly
    # The chat-history read ended up needing no gate at all: a licence-only one
    # still refused it on a plain ai_disabled install, which resolves to
    # Community Edition. ``platform`` is skipped above as the definition site.
