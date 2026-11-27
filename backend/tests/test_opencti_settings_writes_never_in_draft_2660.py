"""OpenCTI settings writes never happen behind a draft (#2660).

A partner run of the Threat Hunt Creator edited the live ``pattern_type_ov``
vocabulary while it staged its hunt package in a draft: OpenCTI drafts stage
knowledge only, so a settings write lands on the live platform outside the
review. Three guards:

* the customization tools refuse every write action while the run holds an
  active draft, nothing sent; reads stay, and outside a draft nothing changes;
* ``list_opencti_vocabulary`` reads a vocabulary with no write path;
* the Hunt Creator, its converter and the autonomous CTEM fleet agents bound to
  OpenCTI deny the settings write tools, and the persona reads the vocabulary.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import app.integrations.providers.opencti.customization as customization_mod
from app.agents.builtin_agents import (
    BUILTIN_AGENTS,
    CTEM_FLEET_AGENTS,
    OPENCTI_SETTINGS_WRITE_TOOLS,
)
from app.integrations.providers.opencti import build_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolDefinition, ToolResult

TOKEN = "https://demo.opencti.io||test-token"
_DRAFT = {"opencti-draft-id": "draft--2660"}
_PERSONA = Path(__file__).resolve().parents[1] / "app/agents/seed_data/agent_persona/opencti-threat-hunt-creator.md"


def _build(
    monkeypatch: pytest.MonkeyPatch, scope_headers: dict[str, str] | None = None
) -> tuple[CallableToolExecutor, list[ToolDefinition], list[str]]:
    sent: list[str] = []

    @contextlib.asynccontextmanager
    async def _client(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        yield None

    async def _gql(
        _client: Any, _url: str, _headers: Any, query: str, *_a: Any, **_k: Any
    ) -> tuple[dict[str, Any], str | None]:
        sent.append(query)
        if "vocabularies(" in query:
            node = {"id": "v1", "name": "kql", "usages": 0, "builtIn": False, "category": {"key": "pattern_type_ov"}}
            return {"vocabularies": {"edges": [{"node": node}]}}, None
        return {}, None

    monkeypatch.setattr(customization_mod, "_client", _client)
    monkeypatch.setattr(customization_mod, "_graphql", _gql)
    executor = CallableToolExecutor()
    tools = build_tools(TOKEN, executor, scope_headers=scope_headers or {}, live_writes=True)
    return executor, tools, sent


async def _run(executor: CallableToolExecutor, tool: str, arguments: dict[str, Any]) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="call-1", name=tool, arguments=arguments))


_WRITES = [
    ("manage_opencti_vocabulary", {"action": "create", "name": "kql", "category": "pattern_type_ov"}),
    ("manage_opencti_vocabulary", {"action": "update", "vocabulary_id": "v1", "name": "kql"}),
    ("manage_opencti_vocabulary", {"action": "delete", "vocabulary_id": "v1"}),
    ("manage_opencti_entity_setting", {"action": "patch", "target_type": "Report", "enforce_reference": True}),
    ("manage_opencti_entity_setting", {"action": "delete_status", "target_type": "Report", "status_id": "s1"}),
    ("manage_opencti_status_template", {"action": "create", "name": "NEW"}),
    ("manage_opencti_decay_rule", {"action": "delete", "decay_rule_id": "d1"}),
    ("manage_opencti_retention_rule", {"action": "delete", "retention_rule_id": "r1"}),
    ("manage_opencti_exclusion_list", {"action": "delete", "exclusion_list_id": "e1"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arguments"), _WRITES)
async def test_a_settings_write_with_an_active_draft_is_refused_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any]
) -> None:
    executor, _, sent = _build(monkeypatch, scope_headers=_DRAFT)
    result = await _run(executor, tool, arguments)
    assert result.content.startswith("Refused:"), result.content
    assert "settings are not staged in a draft" in result.content
    assert "draft--2660" in result.content
    assert "Ask a person" in result.content
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arguments"), _WRITES[:3])
async def test_a_settings_write_outside_a_draft_still_runs(
    monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, Any]
) -> None:
    executor, _, sent = _build(monkeypatch)
    result = await _run(executor, tool, arguments)
    assert not result.content.startswith("Refused:"), result.content
    assert sent, "an admin's settings write outside a draft must reach OpenCTI"


@pytest.mark.asyncio
async def test_a_vocabulary_read_with_an_active_draft_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, _, sent = _build(monkeypatch, scope_headers=_DRAFT)
    result = await _run(executor, "manage_opencti_vocabulary", {"action": "list", "category": "pattern_type_ov"})
    assert "kql (pattern_type_ov)" in result.content
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_list_opencti_vocabulary_reads_the_category(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, tools, sent = _build(monkeypatch, scope_headers=_DRAFT)
    assert "list_opencti_vocabulary" in {t.name for t in tools}
    result = await _run(executor, "list_opencti_vocabulary", {"category": "pattern_type_ov"})
    assert "kql (pattern_type_ov)" in result.content
    assert len(sent) == 1 and "mutation" not in sent[0].lower()


def test_the_seed_deny_list_names_the_customization_writes_of_the_provider() -> None:
    assert set(OPENCTI_SETTINGS_WRITE_TOOLS) == set(customization_mod.CUSTOMIZATION_WRITE_TOOLS)
    assert "list_opencti_vocabulary" not in OPENCTI_SETTINGS_WRITE_TOOLS


@pytest.mark.parametrize("slug", ["opencti-threat-hunt-creator", "opencti-detection-rule-converter"])
def test_the_hunt_agents_deny_the_settings_writes(slug: str) -> None:
    agent = next(a for a in BUILTIN_AGENTS if a["slug"] == slug)
    assert set(OPENCTI_SETTINGS_WRITE_TOOLS) <= set(agent["tool_deny_list"])


def test_the_autonomous_ctem_agents_bound_to_opencti_deny_the_settings_writes() -> None:
    bound = [a for a in CTEM_FLEET_AGENTS if "opencti" in (a.get("integration_template_bindings") or [])]
    assert bound
    for agent in bound:
        assert set(OPENCTI_SETTINGS_WRITE_TOOLS) <= set(agent["tool_deny_list"]), agent["slug"]


def test_the_person_facing_opencti_assistant_keeps_the_settings_tools() -> None:
    agent = next(a for a in BUILTIN_AGENTS if a["slug"] == "opencti-assistant")
    assert not set(OPENCTI_SETTINGS_WRITE_TOOLS) & set(agent.get("tool_deny_list") or [])


def test_the_persona_reads_the_pattern_types_and_never_edits_a_setting() -> None:
    text = _PERSONA.read_text(encoding="utf-8")
    assert "`list_opencti_vocabulary` with `category: pattern_type_ov`" in text
    assert "Never edit a vocabulary or any platform setting." in text
    assert "Never create or edit a vocabulary entry" in text
    assert "taken verbatim" in text
    assert "handle drafts per Rule 10" in text
    assert "10. **Drafts: ask before reusing" in text
