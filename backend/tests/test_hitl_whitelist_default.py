"""Drift guard for the shipped HITL whitelist.

The shipped default names tools as strings. If a tool is renamed and the list
is not updated, the stale entry silently stops matching and that tool starts
gating — which looks like a bug in the gate rather than a stale list, and is
easy to misdiagnose. This test makes the drift loud instead.

It deliberately does NOT assert the list's *contents*. Whether a given tool
belongs in the default is a judgement call that a test cannot make; the point
here is only that every name still corresponds to a real registered tool.
"""

import re
from pathlib import Path

import pytest

from app.services.hitl_whitelist_default import (
    DELIBERATELY_EXCLUDED,
    SHIPPED_DEFAULT_WHITELIST,
)

_BACKEND = Path(__file__).resolve().parents[1]
_TOOL_SOURCES = [
    _BACKEND / "app" / "services" / "builtin_tools",
    _BACKEND / "app" / "services" / "agent_chat.py",
    _BACKEND / "app" / "services" / "general_chat.py",
    _BACKEND / "app" / "services" / "assignment_engine.py",
    _BACKEND / "app" / "tools" / "tool_gating.py",
]

# Matches ToolDefinition(name="…") including the wrapped form where the
# constructor call and the name land on different lines, and the form where a
# ``source=`` keyword comes first (every built-in stamps it).
_NAME_RE = re.compile(r'ToolDefinition\(\s*(?:source=[^,\n]+,\s*)?name="([a-z0-9_]+)"')


#: Tools registered under a CONSTANT rather than a string literal, which the
#: scrape above cannot see: ``ToolDefinition(name=WAIT_TOOL_NAME)``. Imported
#: from the module that defines them, so this is the same single source of
#: truth the registration uses rather than a second spelling to keep in sync.
def _constant_named_tools() -> set[str]:
    from app.services.builtin_tools.platform_admin import (
        DELETE_OAUTH_DEFAULT,
        GET_PLATFORM_AI_SETTINGS,
        LIST_OAUTH_DEFAULTS,
        UPDATE_PLATFORM_AI_SETTINGS,
        UPSERT_OAUTH_DEFAULT,
    )
    from app.services.builtin_tools.platform_admin_credentials import (
        APPLY_OAUTH_DEFAULT_TO_INTEGRATION,
        CREATE_INTEGRATION_APP_SECRET,
    )
    from app.tools.wait_policy import CHECK_TASK_STATUS_TOOL_NAME, WAIT_TOOL_NAME

    return {
        WAIT_TOOL_NAME,
        CHECK_TASK_STATUS_TOOL_NAME,
        GET_PLATFORM_AI_SETTINGS,
        UPDATE_PLATFORM_AI_SETTINGS,
        LIST_OAUTH_DEFAULTS,
        UPSERT_OAUTH_DEFAULT,
        DELETE_OAUTH_DEFAULT,
        CREATE_INTEGRATION_APP_SECRET,
        APPLY_OAUTH_DEFAULT_TO_INTEGRATION,
    }


def _registered_builtin_tool_names() -> set[str]:
    names: set[str] = set()
    for path in _TOOL_SOURCES:
        files = path.rglob("*.py") if path.is_dir() else [path]
        for file in files:
            names.update(_NAME_RE.findall(file.read_text(encoding="utf-8")))
    return names | _constant_named_tools()


@pytest.fixture(scope="module")
def registered_names() -> set[str]:
    names = _registered_builtin_tool_names()
    # Guard the guard: if the scrape breaks, every assertion below would pass
    # vacuously or fail confusingly.
    assert len(names) > 50, f"tool-name scrape looks broken, found {len(names)}"
    return names


def test_every_whitelisted_name_is_a_real_tool(registered_names):
    unknown = sorted(SHIPPED_DEFAULT_WHITELIST - registered_names)
    assert not unknown, (
        "Shipped whitelist names no longer match any registered tool "
        f"(renamed or removed?): {unknown}. A stale entry means the tool now "
        "gates unexpectedly."
    )


def test_every_documented_exclusion_is_a_real_tool(registered_names):
    unknown = sorted(set(DELIBERATELY_EXCLUDED) - registered_names)
    assert not unknown, (
        f"Documented exclusions no longer exist: {unknown}. Remove them so the "
        "list stays an accurate record of what was considered."
    )


def test_exclusions_are_not_also_whitelisted():
    """The two lists must not contradict each other."""
    overlap = sorted(set(DELIBERATELY_EXCLUDED) & SHIPPED_DEFAULT_WHITELIST)
    assert not overlap, f"Listed as both excluded and whitelisted: {overlap}"


def test_every_exclusion_carries_a_reason():
    for name, reason in DELIBERATELY_EXCLUDED.items():
        assert reason.strip(), f"{name} is excluded with no stated reason"


def test_code_interpreter_is_whitelisted_deliberately():
    """Pins the product decision so an accidental removal is visible in a diff.

    ``code_interpreter`` executes model-authored code, so its presence here is
    the single most consequential entry in the shipped default. If this test
    fails because somebody removed it, that may well be correct — but it must be
    a decision, not a drive-by edit.
    """
    assert "code_interpreter" in SHIPPED_DEFAULT_WHITELIST


def test_wait_is_whitelisted_deliberately():
    """Gating the timer means pausing to ask permission to pause.

    ``wait`` sleeps and does nothing else: bounded by ``maximum_wait_time`` and
    ``maximum_total_wait_time`` rather than by the tool budget, interruptible
    from both sides, and a round that only waited does not consume a loop
    iteration. Gated, a delegating agent that waits 20 seconds instead sits in
    ``awaiting_human`` until a person answers — the bounded pause replaced by an
    unbounded one.
    """
    assert "wait" in SHIPPED_DEFAULT_WHITELIST


def test_check_task_status_is_whitelisted_deliberately():
    """The poll beside ``wait``, exempted on the revisit its exclusion invited.

    It reads a status of work the agent already started and does nothing else.
    What once made it look risky — driving a polling loop — is bounded
    elsewhere: its own short, fixed budget the model cannot steer, and the
    iteration waiver it shares with ``wait``. Gated, it asked permission to
    look at whether the work had finished.
    """
    assert "check_task_status" in SHIPPED_DEFAULT_WHITELIST
    assert "check_task_status" not in DELIBERATELY_EXCLUDED


def test_load_tools_is_neither_whitelisted_nor_excluded():
    """It never reaches the gate, so an entry either way would decide nothing.

    The loop answers it inline (``approval.LOOP_HANDLED_TOOLS``) and records
    ``auto_run_meta_not_dispatched``. Whitelisting it would put configuration
    that changes nothing on a screen where every other line changes something.
    """
    from app.tools.approval import LOOP_HANDLED_TOOLS

    assert "load_tools" in LOOP_HANDLED_TOOLS
    assert "load_tools" not in SHIPPED_DEFAULT_WHITELIST
    assert "load_tools" not in DELIBERATELY_EXCLUDED


def test_asking_the_caller_is_not_gated():
    """Like ``request_human_input``: gating a question means asking to ask."""
    assert "ask_the_caller" in SHIPPED_DEFAULT_WHITELIST
