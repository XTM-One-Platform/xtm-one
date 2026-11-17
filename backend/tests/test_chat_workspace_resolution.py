"""A chat turn resolves its workspace from the CONVERSATION.

The acceptance criterion is that a workspace-scoped capability resolves
identically from a flow and from a chat turn. The flow half had a test; this
half did not, and the gap mattered more than a missing test usually does:
`shared_state_namespace` has existed as a parameter of `run_agent_chat` all
along with exactly ONE supplier — `flow_runner` — so chat-side shared state was
un-namespaced and the workspace scope was unreachable from chat entirely.

## Why these assert on source, and why they stopped slicing it

The resolution is a few lines inside a 6000-line function that needs a
database, a live agent, a model and a conversation to call. Faking four
collaborators to reach them would test the fake. So the assertions are about
the WIRING — which referent is read, what consumes it, in what order — which
is what source can show honestly.

The first two cuts of this file sliced the block out by anchoring on its
neighbours, and both broke when the block MOVED for reasons that had nothing
to do with behaviour (first to the point of use, then ahead of both tool
families). Anchoring on a neighbour tests where the code sits; anchoring on
nothing and asserting over the whole function tests what it does. The ordering
check below is the one that would have caught the real bug the second move
introduced — `work_items` reading a local before its assignment — so it earns
its place.
"""

from __future__ import annotations

import inspect
import re
import uuid

import pytest


@pytest.fixture(scope="module")
def src() -> str:
    from app.services.agent_chat import run_agent_chat

    return inspect.getsource(run_agent_chat)


def test_the_referent_is_the_conversation_not_the_agent(src: str) -> None:
    """Nothing inherits a workspace implicitly.

    An agent that silently carried one would make every conversation with it
    write there, whether or not the user meant to. A conversation only has a
    workspace because somebody filed it.
    """
    assert "Conversation.workspace_id" in src
    assert "_scope_conversation_id = safe_uuid(str(conversation_id))" in src
    assert "Conversation.id == _scope_conversation_id" in src
    assert "agent.workspace" not in src
    assert "default_workspace" not in src


def test_a_caller_supplied_namespace_always_wins(src: str) -> None:
    """A flow's `run` / `flow` scope is more specific than the conversation.

    Overwriting it would break the isolation that flow explicitly asked for.
    """
    assert "and shared_state_namespace is None:" in src


def test_the_query_only_runs_when_a_coordination_tool_is_present(src: str) -> None:
    """An ordinary chat turn must not pay for a workspace lookup.

    The first cut queried on every turn, and also put a database dependency
    into the setup phase that the cancellation and trace-failure paths run
    through — which CI caught, because two smoke tests pass a bare object as
    the session precisely because that phase never touched it.
    """
    i = src.index("_workspace_scope_id = None")
    guard = src[i : i + 400]
    assert '"shared_state" in agent_builtin_tool_ids' in guard
    assert '"work_items" in agent_builtin_tool_ids' in guard
    # The workset built-in is workspace-keyed too: dropping it from the guard
    # would leave `_workspace_scope_id` None and collapse every workspace onto
    # one chat corpus, with the rest of the suite still green.
    assert '"workset" in agent_builtin_tool_ids' in guard


def test_the_resolution_precedes_every_consumer(src: str) -> None:
    """The invariant that actually broke.

    Both tool families read `_workspace_scope_id`. The resolution sat before
    the shared-state builder and AFTER the work-items one for one commit, which
    is an `UnboundLocalError` on every turn with that tool — invisible in each
    branch alone and only real once they met. Ordering is asserted, not
    assumed.
    """
    assign = src.index("_workspace_scope_id = None")
    consumers = [
        i
        for i in range(len(src))
        if src.startswith("workspace_id=", i)
        and "_workspace_scope_id" in src[i : i + 80]
    ]
    assert consumers, "nothing consumes the resolved workspace — wiring is gone"
    assert all(assign < c for c in consumers), (
        "a tool builder reads `_workspace_scope_id` before it is assigned"
    )


def test_both_tool_builders_receive_it(src: str) -> None:
    """Shared state and work items must not disagree within one turn."""
    assert "workspace_id=_workspace_scope_id," in src
    assert re.search(
        r"workspace_id=\(\s*session_work_item_workspace_id\s+"
        r"if session_work_item_workspace_id is not None\s+"
        r"else _workspace_scope_id\s*\)",
        src,
    )


def test_the_namespace_is_built_on_the_id_not_the_slug() -> None:
    """A rename must not orphan the state written before it."""
    from app.services.workspaces import workspace_namespace

    wid = uuid.uuid4()
    assert workspace_namespace(wid) == f"workspace:{wid}"


def test_the_flow_runner_is_no_longer_the_only_supplier() -> None:
    """The regression this whole block exists to prevent.

    If `run_agent_chat` stops deriving a namespace itself, the chat path is
    back to depending on `flow_runner` alone, the workspace scope is silently
    unreachable from chat, and every other test here still passes.
    """
    from app.services import agent_chat

    assert "shared_state_namespace = workspace_namespace(" in inspect.getsource(
        agent_chat
    )
