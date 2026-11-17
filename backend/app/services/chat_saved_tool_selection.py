# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""A conversation's saved session tool selection: its format, its writer, its readers.

A send saves the selection the message ran with on its conversation
(``tool_overrides_json``), with the agent it was made for (``agent_id``, null for
the General Assistant): it lists that agent's bindings. A handover, a return to
the source agent or a conversation continued with another agent rebinds
``conversation.agent_id``, and the selection then belongs to the agent before.
Every reader goes through :func:`saved_tool_selection`, which leaves such a
selection aside: the agent now answering runs with its own tools. A selection
saved before the field existed counts as the General Assistant's, the one the web
chat saved it for; one a REST client saved on an agent conversation then is read
as none, and the agent runs with its defaults.

What a turn may still load from it (an id unbound since the save) is decided by
``chat_selection.usable_saved_selection``.
"""

from __future__ import annotations

import json
from typing import Any

#: The key of the saved selection naming the agent it was made for.
SAVED_FOR_KEY = "agent_id"

#: Saved selection key -> the chat services' session kwarg.
_SESSION_KWARGS = (
    ("builtin_tool_ids", "session_builtin_tool_ids"),
    ("custom_tool_ids", "session_custom_tool_ids"),
    ("integration_ids", "session_integration_ids"),
    ("mcp_server_ids", "session_mcp_server_ids"),
    ("knowledge_base_ids", "session_knowledge_base_ids"),
)

#: Session kwarg -> the agent attribute holding that category's defaults.
_AGENT_DEFAULTS = (
    ("session_builtin_tool_ids", "builtin_tool_ids"),
    ("session_custom_tool_ids", "custom_tool_ids"),
    ("session_mcp_server_ids", "mcp_server_ids"),
    ("session_knowledge_base_ids", "knowledge_base_ids"),
)


def saved_for(agent_id: Any) -> str | None:
    """The ``agent_id`` a selection saved for this conversation agent records."""
    return str(agent_id) if agent_id else None


def build_tool_overrides_payload(
    *,
    builtin_tool_ids: list[str] | None,
    custom_tool_ids: list[str] | None,
    integration_ids: list[str] | None,
    mcp_server_ids: list[str] | None,
    knowledge_base_ids: list[str] | None,
    default_tool_ids: list[str] | None,
) -> dict[str, list[str] | None] | None:
    """Build the persisted ``tool_overrides_json`` payload for a conversation.

    Returns ``None`` when the client supplied no actual tool-selection field, so
    callers skip the write and leave any existing overrides untouched. The write
    is gated on the five tool-selection arrays ONLY — ``default_tool_ids`` is
    metadata (a baseline snapshot), not a selection, so a baseline-only payload
    is a no-op: persisting it would normalize every category to ``[]`` and
    disable all tools on reopen, clobbering any previously saved overrides.

    ``default_tool_ids`` is the snapshot of the agent / General Assistant default
    tool set at send time. It is preserved *as-is* — ``None`` stays JSON ``null``
    and is never coerced to ``[]``. The distinction matters on reopen: ``null``
    means "no baseline was recorded" and the frontend falls back to the saved
    enabled set as the baseline, whereas an explicit ``[]`` is treated as an
    authoritative empty baseline. Coercing an omitted baseline to ``[]`` would
    make the reopen merge re-enable every current default (including tools the
    user explicitly disabled for the conversation).
    """
    if (
        builtin_tool_ids is None
        and custom_tool_ids is None
        and integration_ids is None
        and mcp_server_ids is None
        and knowledge_base_ids is None
    ):
        return None
    return {
        "builtin_tool_ids": builtin_tool_ids or [],
        "custom_tool_ids": custom_tool_ids or [],
        "integration_ids": integration_ids or [],
        "mcp_server_ids": mcp_server_ids or [],
        "knowledge_base_ids": knowledge_base_ids or [],
        "default_tool_ids": default_tool_ids,
    }


def persist_tool_selection(
    conv: Any,
    *,
    builtin_tool_ids: list[str] | None,
    custom_tool_ids: list[str] | None,
    integration_ids: list[str] | None,
    mcp_server_ids: list[str] | None,
    knowledge_base_ids: list[str] | None,
    default_tool_ids: list[str] | None,
    scope: str | None = None,
) -> bool:
    """Save the tool selection a message runs with on its conversation.

    The one writer of ``tool_overrides_json``, for the REST send and the web
    chat's turn alike, called once an agent or the General Assistant runs the
    message. The selection records the agent it is made for, so one saved
    before a handover is not read for the agent after it. A selection that is
    the message's alone (``scope == "turn"``, a block's one-off request) saves
    nothing, nor does a send naming no selection. Returns whether it wrote; the
    caller commits.
    """
    if scope == "turn":
        return False
    payload = build_tool_overrides_payload(
        builtin_tool_ids=builtin_tool_ids,
        custom_tool_ids=custom_tool_ids,
        integration_ids=integration_ids,
        mcp_server_ids=mcp_server_ids,
        knowledge_base_ids=knowledge_base_ids,
        default_tool_ids=default_tool_ids,
    )
    if payload is None:
        return False
    conv.tool_overrides_json = json.dumps({**payload, SAVED_FOR_KEY: saved_for(conv.agent_id)})
    return True


def saved_tool_selection(conversation: Any) -> dict[str, Any] | None:
    """The selection saved for the conversation's current agent, or ``None``."""
    raw = getattr(conversation, "tool_overrides_json", None)
    if not raw:
        return None
    try:
        saved = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(saved, dict):
        return None
    if saved.get(SAVED_FOR_KEY) != saved_for(getattr(conversation, "agent_id", None)):
        return None
    return saved


def _agent_default_ids(agent: Any) -> dict[str, list[str]]:
    """Session kwarg -> the ids *agent* enables by default today."""
    from app.services.integration_credentials import flatten_binding_ids

    defaults = {
        kwarg: [str(i) for i in getattr(agent, attribute, None) or [] if i] for kwarg, attribute in _AGENT_DEFAULTS
    }
    defaults["session_integration_ids"] = flatten_binding_ids(getattr(agent, "integration_bindings", None))
    return defaults


def session_tool_kwargs(selection: dict[str, Any] | None, agent: Any = None) -> dict[str, Any]:
    """A saved selection as the chat services' session kwargs.

    Empty without a selection: the turn runs with the agent's (or the General
    Assistant's) defaults. With the *agent* that answers, a default added to it
    since the save (an integration bound, a tool enabled) is enabled too, as the
    web does when it reopens the conversation: the saved selection replaces the
    agent's bindings, so a turn would otherwise list the new integration among
    the connected ones and offer none of its tools. A default the user turned
    off for the conversation is in the baseline (``default_tool_ids``, the
    snapshot stored with the selection; a selection saved before it existed is
    its own baseline) and stays off.
    """
    if not selection:
        return {}
    kwargs = {kwarg: selection.get(key) for key, kwarg in _SESSION_KWARGS}
    if agent is None:
        return kwargs
    snapshot = selection.get("default_tool_ids")
    if isinstance(snapshot, list):
        baseline = {str(i) for i in snapshot}
    else:
        baseline = {str(i) for value in kwargs.values() if isinstance(value, list) for i in value}
    for kwarg, current in _agent_default_ids(agent).items():
        chosen = kwargs.get(kwarg)
        if not isinstance(chosen, list):
            continue
        added = [i for i in current if i not in baseline and i not in chosen]
        if added:
            kwargs[kwarg] = [*chosen, *added]
    return kwargs
