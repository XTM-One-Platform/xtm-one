"""The tag a handover peer is listed with in the system prompt.

An empty ``handover_types`` map is the legacy mode in which the General
Assistant, an agent's chat and an assignment run register every handover tool
for every peer. The ``## Agent Delegation`` block must say so instead of
tagging each peer with the ``delegate`` default of an explicit map, and must not
grow to do it.
"""

import inspect
import pathlib

import pytest

from app.services import agent_chat, assignment_engine, general_chat
from app.services.builtin_tools import handover_peer_tag, offered_handover_types
from app.services.builtin_tools.handover import LEGACY_HANDOVER_TAG
from app.services.mention_dispatch import MENTIONED_AGENT_HANDOVER_TYPES

DELEGATE_INSTRUCTION = "Use `spawn_background_task` to delegate work that runs asynchronously."
INSTRUCTIONS = {
    "consult": "Use `consult_agent` to ask a question and get an answer back (you stay in control).",
    "delegate": DELEGATE_INSTRUCTION,
    "transfer": "Use `transfer_to_agent` to permanently hand over the conversation.",
}
PROMPT_SITES = (general_chat, agent_chat, assignment_engine)


def test_a_peer_of_an_explicit_map_is_tagged_with_its_type():
    assert handover_peer_tag({"a": "consult", "b": "delegate"}, "a", INSTRUCTIONS) == (
        "CONSULT",
        INSTRUCTIONS["consult"],
    )


def test_an_untyped_peer_of_an_explicit_map_runs_as_delegate():
    # Without background tasks such a peer is not listed at all
    # (``reachable_handover_peers``, tests/test_handover_reachable_peers.py).
    assert handover_peer_tag({"a": "consult"}, "b", INSTRUCTIONS) == ("DELEGATE", DELEGATE_INSTRUCTION)


def test_a_type_no_tool_implements_is_tagged_and_told_as_delegate():
    assert handover_peer_tag({"a": "custom"}, "a", INSTRUCTIONS) == ("DELEGATE", DELEGATE_INSTRUCTION)


@pytest.mark.parametrize("empty", [{}, None])
def test_a_peer_of_a_legacy_map_is_told_every_handover_tool(empty):
    tag, instruction = handover_peer_tag(empty, "a", INSTRUCTIONS)
    assert tag == LEGACY_HANDOVER_TAG == "ANY"
    assert instruction == "Use `consult_agent`, `spawn_background_task` or `transfer_to_agent`."


def test_a_legacy_map_without_background_tasks_names_only_the_tools_registered():
    offered = offered_handover_types(delegate_available=False)
    tag, instruction = handover_peer_tag({}, "a", INSTRUCTIONS, offered=offered)
    assert tag == "ANY"
    assert instruction == "Use `consult_agent` or `transfer_to_agent`."
    assert "spawn_background_task" not in instruction


def test_a_mentioned_agent_is_never_told_to_transfer_a_legacy_peer():
    """A mention consults and delegates only (``MENTIONED_AGENT_HANDOVER_TYPES``)."""
    offered = offered_handover_types(allowed_types=MENTIONED_AGENT_HANDOVER_TYPES)
    tag, instruction = handover_peer_tag({}, "a", INSTRUCTIONS, offered=offered)
    assert (tag, instruction) == ("ANY", "Use `consult_agent` or `spawn_background_task`.")


def test_a_legacy_map_names_the_one_tool_left():
    offered = offered_handover_types(allowed_types=frozenset({"transfer"}))
    assert handover_peer_tag({}, "a", INSTRUCTIONS, offered=offered) == ("ANY", "Use `transfer_to_agent`.")


def test_a_denied_tool_is_not_named():
    offered = offered_handover_types(tool_deny_list=["consult_agent"])
    tag, instruction = handover_peer_tag({}, "a", INSTRUCTIONS, offered=offered)
    assert (tag, instruction) == ("ANY", "Use `spawn_background_task` or `transfer_to_agent`.")


@pytest.mark.parametrize(
    "offered",
    [
        offered_handover_types(),
        offered_handover_types(delegate_available=False),
        offered_handover_types(allowed_types=MENTIONED_AGENT_HANDOVER_TYPES),
        offered_handover_types(allowed_types=frozenset({"transfer"})),
    ],
    ids=["every-tool", "no-background-task", "mention", "one-tool"],
)
def test_the_legacy_line_is_not_longer_than_the_delegate_line_it_replaces(offered):
    tag, instruction = handover_peer_tag({}, "a", INSTRUCTIONS, offered=offered)
    assert len(f"[{tag}]") + len(instruction) <= len("[DELEGATE]") + len(DELEGATE_INSTRUCTION)


@pytest.mark.parametrize("module", PROMPT_SITES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_every_prompt_site_tags_its_peers_through_the_helper(module):
    source = pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")
    assert "handover_peer_tag(" in source
    # The tag is no longer derived at the site, where an empty map read as delegate.
    assert "h_type.upper()" not in source
    # The length check above is measured against the instruction each site uses.
    assert f'"delegate": "{DELEGATE_INSTRUCTION}"' in source
