"""get_agent shows the effective self fan-out value next to the stored setting.

The agent setting alone read "Yes" while a background copy of the agent was
refused every spawn: the platform switch and the top-level-run condition were
not shown (dev:harness-gaps get-agent-self-fanout-effective-value).
"""

from __future__ import annotations

import pytest

from app.services.builtin_tools.handover import self_fanout_effective_text


@pytest.fixture
def platform_switch(monkeypatch):
    from app.core.config import settings

    def _set(value: bool) -> None:
        monkeypatch.setattr(settings, "agent_self_fanout_enabled", value)

    return _set


def test_platform_switch_off_wins_over_the_agent_setting(platform_switch):
    platform_switch(False)

    text = self_fanout_effective_text(True)

    assert text.startswith("No")
    assert "AGENT_SELF_FANOUT_ENABLED" in text


def test_agent_setting_off_reads_no(platform_switch):
    platform_switch(True)

    text = self_fanout_effective_text(False)

    assert text.startswith("No")
    assert "this agent" in text


def test_both_on_names_the_top_level_run_condition(platform_switch):
    platform_switch(True)

    text = self_fanout_effective_text(True)

    assert text.startswith("Yes")
    assert "top-level run" in text
    assert "background copy" in text


def test_get_agent_renders_both_lines():
    import inspect

    from app.services.builtin_tools import agent_builder

    source = inspect.getsource(agent_builder)

    assert "Parallel copies of itself (agent setting)" in source
    assert "Parallel copies of itself (effective)" in source
    assert "self_fanout_effective_text(" in source
