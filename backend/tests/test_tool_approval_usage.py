"""Whether tool approvals are in use, and who asks.

Every screen that shows an approval state reads ``approvals_in_use`` through
``GET /tool-approvals``, and Settings > Access and security > Tool approvals lists the
asking agents through ``GET /tool-approvals/usage``. Both fold the switches the
way the gate does (``hitl_whitelist.agent_asks``), so a platform where no agent
asks shows no approval anywhere.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import tool_approvals as api
from app.services.hitl_whitelist import agent_asks, install_default_asks
from app.services.tool_approval_usage import approvals_in_use, summarize_approval_usage


class _SavepointDb:
    """Stored switches ``(tool_approvals_enabled, approve_tool_calls_default)``, or a failing read."""

    def __init__(self, switches=(None, None), *, fail=False):
        self.switches = switches
        self.fail = fail
        self.savepoint_rolled_back = False

    def begin_nested(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, *_exc):
        self.savepoint_rolled_back = exc_type is not None
        return False

    async def execute(self, _stmt):
        if self.fail:
            raise RuntimeError("database unavailable")
        return SimpleNamespace(first=lambda: self.switches)

    async def rollback(self):
        raise AssertionError("must not roll back the caller's session")


def _usage(*, enabled=True, default=False, agents=(), assistant=False):
    return summarize_approval_usage(
        approvals_enabled=enabled,
        install_default=default,
        agents=[(uuid.uuid4(), name, own) for name, own in agents],
        general_assistant_available=assistant,
    )


@pytest.mark.parametrize(("stored", "expected"), [(True, True), (False, False), (None, False)])
def test_the_install_default_is_opt_in(stored, expected):
    assert install_default_asks(stored) is expected


@pytest.mark.parametrize(
    ("own", "default", "expected"),
    [(True, False, True), (False, True, False), (None, True, True), (None, False, False)],
)
def test_an_agents_own_setting_wins_over_the_default(own, default, expected):
    assert agent_asks(own, default) is expected


def test_an_upgraded_install_is_not_in_use():
    usage = _usage(agents=[("Mail Desk", None), ("Relay", None)], assistant=True)
    assert usage.in_use is False
    assert usage.configured is False
    payload = usage.to_payload()
    assert payload["asking_agents"] == []
    assert payload["agents_following_default"] == 2
    assert payload["general_assistant_asks"] is False


def test_one_agent_set_to_ask_puts_approvals_in_use():
    usage = _usage(agents=[("Mail Desk", True), ("Relay", None), ("Scout", False)])
    assert usage.in_use is True
    payload = usage.to_payload()
    assert [a["name"] for a in payload["asking_agents"]] == ["Mail Desk"]
    assert payload["asking_agents"][0]["follows_default"] is False
    assert (payload["agents_set_to_ask"], payload["agents_set_not_to_ask"]) == (1, 1)


def test_the_default_on_makes_every_agent_that_sets_nothing_ask():
    usage = _usage(default=True, agents=[("Relay", None), ("Scout", False)], assistant=True)
    assert [a.name for a in usage.asking_agents] == ["Relay"]
    assert usage.asking_agents[0].own is None
    assert usage.general_assistant_asks is True


def test_the_general_assistant_alone_puts_approvals_in_use():
    usage = _usage(default=True, assistant=True)
    assert usage.in_use is True
    assert usage.to_payload()["asking_agents"] == []


@pytest.mark.parametrize(("assistant", "default"), [(True, False), (False, True), (False, False)])
def test_the_payload_says_whether_the_general_assistant_is_there_to_ask(assistant, default):
    """The default switch's wording names the General Assistant only where chat is available."""
    payload = _usage(default=default, agents=[("Relay", None)], assistant=assistant).to_payload()
    assert payload["general_assistant_available"] is assistant
    assert payload["general_assistant_asks"] is (assistant and default)


def test_without_chat_the_default_on_puts_only_the_agents_in_use():
    assert _usage(default=True, assistant=False).in_use is False
    assert _usage(default=True, agents=[("Relay", None)], assistant=False).in_use is True


def test_suspended_approvals_are_configured_but_not_in_use():
    usage = _usage(enabled=False, agents=[("Mail Desk", True)])
    assert usage.configured is True
    assert usage.in_use is False
    assert usage.to_payload()["suspended"] is True


def test_no_settings_row_reads_as_available_and_opt_in():
    usage = _usage(enabled=None, default=None, agents=[("Relay", None)], assistant=True)
    assert usage.suspended is False
    assert usage.asks_by_default is False
    assert usage.in_use is False


async def test_an_unreadable_answer_is_in_use_and_leaves_the_callers_session_alone():
    """``GET /tool-approvals`` has loaded the user and their approvals before asking."""
    db = _SavepointDb(fail=True)
    assert await approvals_in_use(db) is True
    assert db.savepoint_rolled_back is True


async def test_suspended_approvals_are_not_in_use():
    db = _SavepointDb((False, True))
    assert await approvals_in_use(db) is False
    assert db.savepoint_rolled_back is False


def test_the_usage_route_is_for_administrators_only():
    from app.api.deps import require_admin

    signature = inspect.signature(api.tool_approval_usage)
    assert signature.parameters["user"].default.dependency is require_admin


def test_the_profile_read_reports_whether_approvals_are_in_use():
    source = inspect.getsource(api.list_my_tool_approvals)
    assert "approvals_in_use(db)" in source
    assert '"approvals_in_use": in_use' in source
