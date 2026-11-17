"""The platform group offers a caller only what the caller may run.

Sam's decision 2 (2 Nov 2026): the platform group (``agent_builder``,
``fleets``, ``flow_builder``) stays on by default on the default assistants,
and its functions are scoped by the user's own permissions at run time: a
function only an administrator may run is never offered to anyone else,
instead of being refused after the call. No checkbox decides it.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.agent_chat import _add_platform_builder_tools
from app.services.builtin_tool_aliases import (
    PLATFORM_ADMIN_FUNCTIONS,
    alias_group,
    alias_scoped_functions,
    caller_may_run,
)
from app.tools.base import CompositeToolExecutor


def _offered(*, caller_is_admin: bool) -> set[str]:
    composite = CompositeToolExecutor()
    _add_platform_builder_tools(
        composite,
        None,
        str(uuid.uuid4()),
        ["agent_builder"],
        caller_is_admin=caller_is_admin,
    )
    names = {d.name for d in composite.all_tools}
    # Neither offered nor routable: the executor never reaches the handler.
    assert set(composite.tool_names) >= names
    return names


def test_every_admin_only_function_is_a_function_of_the_platform_group():
    granted = {fn for alias in ("agent_builder", "fleets", "flow_builder") for fn in alias_scoped_functions(alias)}
    assert granted >= PLATFORM_ADMIN_FUNCTIONS
    assert {alias_group(a) for a in ("agent_builder", "fleets", "flow_builder")} == {"platform"}


def test_caller_may_run():
    assert caller_may_run("cancel_runs", is_admin=True)
    assert not caller_may_run("cancel_runs", is_admin=False)
    assert caller_may_run("create_agent", is_admin=False)


# The evaluation tools are built while evaluation is on; with it off they are
# withheld whoever asks, which would hide the decision.
@pytest.mark.usefixtures("evaluation_enabled")
def test_a_non_admin_turn_is_not_offered_the_admin_only_functions():
    offered = _offered(caller_is_admin=False)
    assert not offered & PLATFORM_ADMIN_FUNCTIONS
    # The rest of the group stays: the handlers scope it per entity.
    assert {"create_agent", "update_agent", "list_runs", "get_agent_feedback"} <= offered


@pytest.mark.usefixtures("evaluation_enabled")
def test_an_admin_turn_is_offered_them():
    offered = _offered(caller_is_admin=True)
    assert offered >= PLATFORM_ADMIN_FUNCTIONS
    assert "create_agent" in offered


def test_the_default_is_the_narrower_surface():
    """A caller nobody said is an administrator gets the non-admin surface."""
    composite = CompositeToolExecutor()
    _add_platform_builder_tools(composite, None, str(uuid.uuid4()), ["agent_builder"])
    assert "cancel_runs" not in {d.name for d in composite.all_tools}
