"""Every tool a seeded skill or persona tells the agent to call is registered.

A seeded skill or persona that names a tool nobody answers to sends the model
after a call it cannot make, and a renamed or removed tool leaves that text
stale silently. The CTI pack has carried this check since it shipped
(``tests/test_cti_pack.py``); this one widens it to every markdown file under
``seed_data/skill_content`` and ``seed_data/agent_persona``.

A name counts as known when an integration builder, a built-in tool group or
the agent loop registers it, or when an integration declares it as an alias
(Brave Search answers the Google Search tool names).
"""

from __future__ import annotations

import re

from app.agents.seed import SEED_DATA_DIR

#: A backticked snake_case name led by a verb, as the content cites a tool.
_TOOL_CITATION = re.compile(
    r"`((?:get|list|search|lookup|query|check|create|add|update|delete|manage|run|"
    r"submit|scan|count|find|enrich|download|export|import|promote|merge|upload|remove|"
    r"resolve|publish|validate|request|apply|dispatch|generate|render|fetch|load|store|"
    r"invalidate|complete|claim|schedule|cancel|reschedule|spawn|consult|inspect|record|"
    r"dispose|send|reply|forward|read|write|edit|attach|detach|bind|unbind|archive|"
    r"approve|start|stop|duplicate)_[a-z0-9_]+)`"
)


#: Backticked names shaped like a tool that the content uses as something else:
#: ``edit_presentation`` operations, video scene fields and record date fields.
_NOT_TOOLS = {
    "update_block",
    "update_slide",
    "delete_block",
    "delete_slide",
    "read_only",
    "list_build",
    "start_frame_and_reference_images",
    "start_frame_file_id",
    "validate_only",
    "start_date",
    "start_time",
    "stop_time",
    # ``set_shared_state`` list operations and its retention argument.
    "archive_key",
    "remove_json",
    "update_json",
    # A ``manage_openaev_scenario`` action (the CTEM orchestrators set the repeat with it).
    "schedule_recurrence",
}


def _seeded_texts() -> dict[str, str]:
    texts: dict[str, str] = {}
    for folder in ("skill_content", "agent_persona"):
        for path in sorted((SEED_DATA_DIR / folder).glob("*.md")):
            texts[f"{folder}/{path.name}"] = path.read_text(encoding="utf-8")
    return texts


def _integration_tool_names() -> set[str]:
    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import CHANNEL_BOT_TOOLS, PROVIDER_TOOLS
    from app.tools.base import CallableToolExecutor

    names: set[str] = set()
    for provider, builder in {**PROVIDER_TOOLS, **CHANNEL_BOT_TOOLS}.items():
        # Every builder answers its dummy (``tests/test_dummy_token_shapes.py``).
        defs = builder(build_dummy_token(provider), CallableToolExecutor(), refresh_fn=lambda: None)
        for definition in defs:
            if definition.name:
                names.add(definition.name)
            names.update(getattr(definition, "aliases", None) or ())
    # The tools an opt-in integration setting adds (OpenCTI Live writes:
    # apply_opencti_draft, create_opencti_label) exist only when it is on.
    opencti = PROVIDER_TOOLS["opencti"]
    names.update(
        d.name
        for d in opencti(build_dummy_token("opencti"), CallableToolExecutor(), refresh_fn=lambda: None, live_writes=True)
        if d.name
    )
    return names


def _builtin_tool_names() -> set[str]:
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    return {
        definition["name"]
        for definitions in build_builtin_tool_catalog(include_disabled=True).values()
        for definition in definitions
    }


def _agent_loop_tool_names() -> set[str]:
    """Tools a turn adds from an agent's columns or for every agent, outside the groups."""
    from app.services.builtin_tools import (
        CTEM_CASE_TOOL_NAMES,
        CTEM_SIGNAL_TOOL_NAMES,
        ORGANIZATION_CONTEXT_TOOL,
        SCHEDULING_TOOL_NAMES,
    )
    from app.services.evaluation import always_ask
    from app.tools.tool_gating import LOAD_TOOLS_TOOL_NAME
    from app.tools.wait_policy import CHECK_TASK_STATUS_TOOL_NAME, WAIT_TOOL_NAME

    return {
        # The loop answers these itself, never through a group or the executor.
        LOAD_TOOLS_TOOL_NAME,
        WAIT_TOOL_NAME,
        CHECK_TASK_STATUS_TOOL_NAME,
        *always_ask._KNOWLEDGE_TOOLS,
        *always_ask._KNOWLEDGE_WRITE_TOOLS,
        *always_ask._MEMORY_TOOLS,
        *always_ask._HANDOVER_TOOLS,
        *always_ask._SKILL_TOOLS,
        ORGANIZATION_CONTEXT_TOOL,
        # Offered by agent slug to the seeded CTEM agents (``offers_ctem_case_tools``), outside the groups.
        *CTEM_CASE_TOOL_NAMES,
        *CTEM_SIGNAL_TOOL_NAMES,
        # One-off scheduled work: added per turn by the chat and the assignment engine (``build_scheduling_tools``).
        *SCHEDULING_TOOL_NAMES,
    }


def test_the_citation_pattern_reads_a_backticked_tool_name():
    assert _TOOL_CITATION.findall("call `list_opencti_labels`, then `apply_opencti_draft`") == [
        "list_opencti_labels",
        "apply_opencti_draft",
    ]


def test_every_tool_a_seeded_skill_or_persona_cites_is_registered():
    known = _integration_tool_names() | _builtin_tool_names() | _agent_loop_tool_names()
    unknown = sorted(
        {
            f"{where}: {name}"
            for where, text in _seeded_texts().items()
            for name in _TOOL_CITATION.findall(text)
            if name not in known and name not in _NOT_TOOLS
        }
    )
    assert not unknown, "\n".join(unknown)
