"""``load_tools`` says why an integration's tool is missing, never just "unknown" (#11866).

A tool an earlier turn had can belong to an integration this run left out
(a personal integration on a company-managed agent, a colleague's unshared
token, an account not connected): its tools are never built, so the name
resolves to nothing. The gate names the left-out integrations with their
reason next to the unknown name.
"""

from __future__ import annotations

from typing import Any

from app.services.integration_credentials import (
    INTEGRATION_NOT_VISIBLE_REASON,
    NOT_COMPANY_MANAGED_REASON,
    OAUTH_NOT_SHARED_REASON,
    unavailable_integration_reasons,
)
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ScopedToolExecutor, ToolDefinition
from app.tools.tool_gating import build_tool_gate


def _tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(name=f"notion_tool_{i}", description=f"notion {i}", source="integration:notion")
        for i in range(10)
    ]


def _entries() -> list[Any]:
    return [
        {
            "provider": "oauth_a2a",
            "integration_id": "i-1",
            "integration_name": "Partner A2A",
            "reason": NOT_COMPANY_MANAGED_REASON,
        },
        {
            "provider": "jira",
            "integration_id": "i-2",
            "integration_name": "Secret Jira",
            "reason": INTEGRATION_NOT_VISIBLE_REASON,
        },
        {
            "provider": "github",
            "integration_id": "i-3",
            "integration_name": "Ana GitHub",
            "reason": OAUTH_NOT_SHARED_REASON,
        },
        {"provider": "slack", "integration_id": "i-4", "reason": "something_else"},
        "not a dict",
    ]


def test_reasons_label_each_left_out_integration_and_hide_invisible_names() -> None:
    reasons = unavailable_integration_reasons(_entries())
    assert set(reasons) == {"Partner A2A", "jira", "Ana GitHub"}
    assert "company-managed" in reasons["Partner A2A"]
    assert "cannot see" in reasons["jira"]
    assert "Secret Jira" not in " ".join([*reasons, *reasons.values()])
    assert unavailable_integration_reasons(None) == {}


def test_load_tools_names_the_left_out_integration_for_an_unknown_tool() -> None:
    composite = CompositeToolExecutor()
    composite.withhold_integrations(unavailable_integration_reasons(_entries()))
    gate = build_tool_gate(_tools(), enabled=True, min_tools=1, withheld_integrations=composite.withheld_integrations)
    assert gate is not None
    reveal = gate.reveal([], ["delegate_to_partner_target"])
    assert reveal.unknown_tools == ["delegate_to_partner_target"]
    text = gate.describe_reveal(reveal)
    assert "Unknown tool name(s) ignored: delegate_to_partner_target." in text
    assert '"Partner A2A"' in text and "company-managed" in text
    assert "not available in this run" in text


def test_a_known_tool_or_a_run_without_left_out_integrations_says_nothing_more() -> None:
    composite = CompositeToolExecutor()
    composite.withhold_integrations(unavailable_integration_reasons(_entries()))
    gate = build_tool_gate(_tools(), enabled=True, min_tools=1, withheld_integrations=composite.withheld_integrations)
    assert gate is not None
    text = gate.describe_reveal(gate.reveal([], ["notion_tool_1"]))
    assert "not available in this run" not in text

    plain = build_tool_gate(_tools(), enabled=True, min_tools=1)
    assert plain is not None
    text = plain.describe_reveal(plain.reveal([], ["no_such_tool"]))
    assert "Unknown tool name(s) ignored: no_such_tool." in text
    assert "not available in this run" not in text


async def _ok(**_: Any) -> str:
    return "ok"


def test_a_scoped_executor_exposes_the_left_out_integrations() -> None:
    composite = CompositeToolExecutor()
    executor = CallableToolExecutor()
    executor.register("notion_tool_0", _ok)
    composite.add_tools(_tools()[:1], executor)
    composite.withhold_integrations({"Partner A2A": "reason"})
    scoped = ScopedToolExecutor(composite, composite.all_tools)
    assert scoped.withheld_integrations == {"Partner A2A": "reason"}
