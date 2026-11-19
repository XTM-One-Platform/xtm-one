"""Which seeded agents a person can pick in chat.

The seed is the source of truth of ``disable_chat`` (``seed._sync_agent_fields``
reads a missing key as chat on), so an agent added without the key lands at the
top of every user's chat picker, in the LLM proxy model list and in the MCP
exposure. Chat stays on only for the agents a person gets value from talking
to; every other one (JSON or schema output, a ``skip_llm`` pipeline, an intent
or playbook target, a run orchestrator, a transfer or consult target) is
``disable_chat``. It stays invocable by slug on ``/api/v1/platform/chat/messages``,
by delegation, consult and transfer, and by its assignments: none of them read
the flag.
"""

from __future__ import annotations

from app.agents.builtin_agents import BUILTIN_AGENTS

#: The seeded agents open to chat, each for the person it serves. A new entry
#: here is a product decision: say who talks to it.
CHAT_AGENTS: dict[str, str] = {
    "xtm-one-assistant": "the platform's own help",
    "ctem-assistant": "the XTM One entry agent",
    "opencti-assistant": "the OpenCTI embedded chat",
    "openaev-assistant": "the OpenAEV embedded chat",
    "opencrq-assistant": "the OpenCRQ embedded chat",
    "opencti-dashboard-creator": "plans a dashboard with the person",
    "opencti-threat-hunt-creator": "builds a hunt with the person",
    "openaev-scenario-creator": "builds a scenario with the person",
    "fimi-analyst": "the FIMI vertical chat",
    "deep-investigation-agent": "an investigation a person asks for",
    "template-designer": "designs a template with the person",
    "deck-importer": "imports a deck the person brings",
    "ctem-posture-orchestrator": "answers where the organization stands",
}


def _chat_on(agent: dict) -> bool:
    return not agent.get("disable_chat", False)


def test_only_the_listed_agents_are_open_to_chat():
    open_to_chat = {a["slug"] for a in BUILTIN_AGENTS if _chat_on(a)}
    assert open_to_chat == set(CHAT_AGENTS), (
        f"open to chat but not listed: {sorted(open_to_chat - set(CHAT_AGENTS))}; "
        f"listed but chat off: {sorted(set(CHAT_AGENTS) - open_to_chat)}"
    )


def test_machine_output_agents_are_never_open_to_chat():
    machine = [
        a["slug"]
        for a in BUILTIN_AGENTS
        if _chat_on(a) and (a.get("output_format") == "json" or a.get("output_schema") or a.get("skip_llm"))
    ]
    assert not machine, f"set disable_chat on: {machine}"


def test_every_listed_chat_agent_is_seeded():
    seeded = {a["slug"] for a in BUILTIN_AGENTS}
    assert set(CHAT_AGENTS) <= seeded, sorted(set(CHAT_AGENTS) - seeded)
