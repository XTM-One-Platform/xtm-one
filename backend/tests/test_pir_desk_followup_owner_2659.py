"""#2659: a PIR desk follow-up item has an owner whose inbox takes it; an owner-less item warns.

The PIR Desk Orchestrator filed unfinished hunt packages as work items with no owner and
no needs_human, which no inbox ever took. The seed now gives it a ``Follow-up due``
work-item assignment on ``opencti:pir-desk:followup:``, both triage prompts tell it to
use it, and ``create_work_item`` warns an agent no inbox drains that creates such an item.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.agents import opencti_pir_desk as pir
from app.agents.builtin_agents import ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS, BUILTIN_ASSIGNMENTS
from app.agents.gate_answered import INHERIT_SWITCH_FROM_KEY
from app.services import work_item_context as ctx
from app.services.builtin_tools import build_work_item_tools
from app.services.builtin_tools import work_items as wi_tools
from app.tools.base import ToolCall


def _seeded(name: str) -> dict:
    return next(a for a in BUILTIN_ASSIGNMENTS if a["agent_slug"] == pir.ORCHESTRATOR and a["name"] == name)


def test_the_followup_inbox_is_seeded_on_its_prefix_and_inherits_the_desk_switch():
    inbox = _seeded(pir.FOLLOWUP_DUE)
    assert inbox["trigger_type"] == "work_item"
    assert inbox["trigger_config"]["correlation_prefix"] == pir.FOLLOWUP_PREFIX == "opencti:pir-desk:followup:"
    assert inbox["enabled"] is False and inbox["admin_opt_in"] is True
    assert (pir.ORCHESTRATOR, pir.FOLLOWUP_DUE) in ADMIN_OPT_IN_BUILTIN_ASSIGNMENTS
    # An instance whose desk is already on gets it on upgrade, switched on like its sibling.
    sibling = inbox[INHERIT_SWITCH_FROM_KEY]
    assert any(a["agent_slug"] == pir.ORCHESTRATOR and a["name"] == sibling for a in BUILTIN_ASSIGNMENTS)
    assert "staged draft" in inbox["prompt"] and "needs_human" in inbox["prompt"]


def test_the_approval_gate_is_unchanged_and_never_takes_a_followup():
    gate = _seeded("Gate answered")
    prefixes = repr(gate["trigger_config"])
    assert pir.APPROVAL_PREFIX in prefixes
    assert pir.FOLLOWUP_PREFIX not in prefixes
    assert not pir.FOLLOWUP_PREFIX.startswith(pir.APPROVAL_PREFIX)
    assert not pir.APPROVAL_PREFIX.startswith(pir.FOLLOWUP_PREFIX)


@pytest.mark.parametrize("name", ["PIR match created", pir.REPORT_CONTAINED])
def test_both_triage_prompts_never_leave_an_item_without_owner_or_ask(name):
    prompt = _seeded(name)["prompt"]
    assert pir.FOLLOWUP_PREFIX in prompt
    assert "owner_agent_id yourself" in prompt
    assert "needs_human true" in prompt
    assert "Never create a work item with no owner and no ask." in prompt


def test_the_persona_names_the_followup_rule():
    from pathlib import Path

    persona = Path(pir.__file__).resolve().parent / "seed_data" / "agent_persona" / f"{pir.ORCHESTRATOR}.md"
    text = persona.read_text(encoding="utf-8")
    assert "opencti:pir-desk:followup:" in text and "**Follow-up due**" in text
    assert "never create one" in text


# ── create_work_item: the owner-less warning ─────────────────────────────────


async def _create(monkeypatch, *, has_inbox, owner=None, needs_human=False):
    async def _fake_create(**kwargs):
        return {
            "id": "wi-1",
            "title": kwargs["title"],
            "status": "blocked" if needs_human else "open",
            "priority": "medium",
            "owner_agent_id": kwargs.get("owner_agent_id"),
            "needs_human": needs_human,
        }

    async def _pickup(item):
        return "automatic"

    asked: list = []

    async def _has_inbox(agent_id):
        asked.append(agent_id)
        return has_inbox

    monkeypatch.setattr(wi_service, "create_work_item", _fake_create)
    monkeypatch.setattr(ctx, "new_item_pickup_state", _pickup)
    monkeypatch.setattr(ctx, "agent_has_work_item_inbox", _has_inbox)
    agent = str(uuid.uuid4())
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), agent)
    arguments: dict = {"title": "Finish the hunt package"}
    if owner:
        arguments["owner_agent_id"] = owner
    result = await executor.execute_tool(ToolCall(id="c1", name="create_work_item", arguments=arguments))
    return result.content, asked


@pytest.mark.asyncio
async def test_an_owner_less_item_from_an_agent_with_no_inbox_answers_the_warning(monkeypatch):
    content, asked = await _create(monkeypatch, has_inbox=False)
    assert not content.startswith("Error")
    assert "nothing will ever take this item" in content
    assert "set owner_agent_id to an agent whose work-item inbox takes" in content
    assert "needs_human true with an ask card" in content
    assert asked


@pytest.mark.parametrize("has_inbox", [True, None], ids=["has-inbox", "unreadable"])
@pytest.mark.asyncio
async def test_no_warning_when_the_agent_has_an_inbox_or_it_cannot_be_read(monkeypatch, has_inbox):
    content, _ = await _create(monkeypatch, has_inbox=has_inbox)
    assert "nothing will ever take this item" not in content


@pytest.mark.asyncio
async def test_an_owned_item_never_reads_the_inboxes_for_the_warning(monkeypatch):
    content, asked = await _create(monkeypatch, has_inbox=False, owner=str(uuid.uuid4()))
    assert "nothing will ever take this item" not in content
    assert asked == []


def test_the_tool_description_no_longer_reads_empty_owner_as_own_backlog():
    defs, _ = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    create = next(d for d in defs if d.name == "create_work_item")
    assert "leave it empty to keep the item in your own backlog" not in create.description
    assert "your own id to keep the item in your own backlog" in create.description
    assert "routed to nobody" in create.description
    assert wi_tools._NO_OWNER_NO_INBOX_WARNING.startswith("  - Warning:")


@pytest.mark.asyncio
async def test_with_a_roster_the_agent_may_own_its_own_item(monkeypatch):
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return {"id": "wi-1", "title": kwargs["title"], "status": "open", "priority": "medium"}

    async def _pickup(item):
        return "automatic"

    monkeypatch.setattr(wi_service, "create_work_item", _fake_create)
    monkeypatch.setattr(ctx, "new_item_pickup_state", _pickup)
    agent = str(uuid.uuid4())
    peers = [{"id": str(uuid.uuid4()), "name": "Peer", "slug": "peer"}]
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), agent, peer_agents=peers)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="create_work_item", arguments={"title": "x", "owner_agent_id": agent})
    )
    assert not result.content.startswith("Error"), result.content
    assert captured["owner_agent_id"] == agent
