"""An unrecognised coordination scope fails closed.

This is a regression test for a fail-OPEN that existed before the workspace
scope did. `FlowWalker._wired_coordination` resolved the shared-state key
namespace as:

    if scope == "run":   namespace = f"flow-run:{run.id}"
    elif scope == "flow": namespace = f"flow:{run.flow_id}"

with **no else**. An unrecognised scope therefore left `namespace` as None —
and None means raw, un-namespaced keys, which is the global blackboard. So a
scope nobody recognised silently produced the LEAST isolated behaviour
available.

It was narrow: the graph validator treats a static select as an enum and
rejects out-of-range values, so reaching it took hand-written JSON or a direct
API caller. It stops being tolerable once one of the scopes exists
specifically to isolate, because the fallback is the exact shape the feature
promises never to take.

Driven through the real method with a stub run rather than a full flow: what
is under test is the branch, and a fixture that needed a database and a
running flow would test the harness.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core.scopes import (
    COORDINATION_SCOPE_FLOW,
    COORDINATION_SCOPE_GLOBAL,
    COORDINATION_SCOPE_RUN,
    COORDINATION_SCOPE_WORKSPACE,
)
from app.services.flow_runner import FlowWalker
from app.services.workspaces import WorkspaceScopeError

AGENT_NODE = "agent-1"
CAP_NODE = "cap-1"


def _runner(scope: str, *, workspace_id: str | None = None) -> FlowWalker:
    """A FlowWalker with just enough state for `_wired_coordination`.

    Constructed without `__init__` on purpose: the real one wants a database,
    a user and a persisted run, none of which this branch reads.
    """
    runner = FlowWalker.__new__(FlowWalker)
    config: dict[str, str] = {"scope": scope}
    if workspace_id is not None:
        config["workspace_id"] = workspace_id
    runner.graph = {
        "nodes": [
            {"id": AGENT_NODE, "kind": "agent"},
            {"id": CAP_NODE, "kind": "capability.shared_state", "config": config},
        ],
        "edges": [
            {
                "id": "e1",
                "source": CAP_NODE,
                "target": AGENT_NODE,
                "target_handle": "shared_state",
            }
        ],
    }
    runner.nodes = {n["id"]: n for n in runner.graph["nodes"]}
    runner.run = SimpleNamespace(id=uuid.uuid4(), flow_id=uuid.uuid4())
    return runner


def test_run_and_flow_still_namespace_as_before() -> None:
    """The fix must not change the two scopes that already worked."""
    runner = _runner(COORDINATION_SCOPE_RUN)
    _tools, namespace, _peers, workspace_ref, _wi_ws = runner._wired_coordination(
        AGENT_NODE, None
    )
    assert namespace == f"flow-run:{runner.run.id}"
    assert workspace_ref is None

    runner = _runner(COORDINATION_SCOPE_FLOW)
    _tools, namespace, _peers, workspace_ref, _wi_ws = runner._wired_coordination(
        AGENT_NODE, None
    )
    assert namespace == f"flow:{runner.run.flow_id}"
    assert workspace_ref is None


def test_global_still_means_no_namespace() -> None:
    """`global` is the one scope for which an absent namespace is CORRECT."""
    runner = _runner(COORDINATION_SCOPE_GLOBAL)
    _tools, namespace, _peers, workspace_ref, _wi_ws = runner._wired_coordination(
        AGENT_NODE, None
    )
    assert namespace is None
    assert workspace_ref is None


def test_an_unknown_scope_raises_instead_of_falling_back_to_global() -> None:
    """The regression. Before the fix this returned `namespace=None`.

    The assertion is specifically that it does NOT return — a returned None is
    indistinguishable from the legitimate `global` case above, which is
    precisely why the bug was invisible.
    """
    runner = _runner("workspac")  # a plausible typo, not a random string
    with pytest.raises(WorkspaceScopeError) as exc:
        runner._wired_coordination(AGENT_NODE, None)
    assert exc.value.reason == "unknown_scope"
    assert "workspac" in str(exc.value)


def test_the_workspace_scope_defers_resolution_to_the_caller() -> None:
    """No namespace here, and the id handed out for the async caller.

    Split this way so that ignoring `workspace_ref` yields NO namespace rather
    than a silently global one — the failure is impossible to skip.
    """
    wid = str(uuid.uuid4())
    runner = _runner(COORDINATION_SCOPE_WORKSPACE, workspace_id=wid)
    tools, namespace, _peers, workspace_ref, _wi_ws = runner._wired_coordination(
        AGENT_NODE, None
    )
    assert "shared_state" in tools
    assert namespace is None
    assert workspace_ref == wid


def test_the_workspace_scope_with_no_workspace_defers_an_empty_ref() -> None:
    """Empty, not None — so the caller resolves it and fails with `missing`.

    Returning None would make this indistinguishable from `global`, which is
    the same trap the unknown-scope branch fell into.
    """
    runner = _runner(COORDINATION_SCOPE_WORKSPACE)
    _tools, namespace, _peers, workspace_ref, _wi_ws = runner._wired_coordination(
        AGENT_NODE, None
    )
    assert namespace is None
    assert workspace_ref == ""


def test_an_explicit_global_wired_first_is_not_overridden() -> None:
    """First wired wins, `global` included.

    `global` resolves to no namespace, so a guard reading "not scoped yet" off
    the namespace let a second wired node replace an explicit `global`.
    """
    runner = _runner(COORDINATION_SCOPE_GLOBAL)
    runner.graph["nodes"].append(
        {
            "id": "cap-2",
            "kind": "capability.shared_state",
            "config": {"scope": COORDINATION_SCOPE_WORKSPACE, "workspace_id": str(uuid.uuid4())},
        }
    )
    runner.graph["edges"].append(
        {"id": "e2", "source": "cap-2", "target": AGENT_NODE, "target_handle": "shared_state"}
    )
    runner.nodes = {n["id"]: n for n in runner.graph["nodes"]}
    _tools, namespace, _peers, workspace_ref, _wi_ws = runner._wired_coordination(
        AGENT_NODE, None
    )
    assert namespace is None
    assert workspace_ref is None


def test_an_unknown_workset_scope_raises() -> None:
    runner = _runner(COORDINATION_SCOPE_RUN)
    runner.graph["nodes"][1] = {
        "id": CAP_NODE,
        "kind": "capability.workset",
        "config": {"scope": "workspac"},
    }
    runner.graph["edges"][0]["target_handle"] = "workset"
    runner.nodes = {n["id"]: n for n in runner.graph["nodes"]}
    with pytest.raises(WorkspaceScopeError) as exc:
        runner._wired_workset(AGENT_NODE)
    assert exc.value.reason == "unknown_scope"


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["", None])
async def test_an_empty_workspace_id_is_refused_as_missing(ref: str | None) -> None:
    """What the builder form sends for a cleared select: refused before any read."""
    from app.services.workspaces import resolve_workspace_scope

    with pytest.raises(WorkspaceScopeError) as exc:
        await resolve_workspace_scope(None, SimpleNamespace(id=uuid.uuid4()), ref)
    assert exc.value.reason == "missing"
