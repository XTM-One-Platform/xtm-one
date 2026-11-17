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

"""What a chat request may add to the tools its turn loads.

A chat turn loads the MCP servers, custom tools, knowledge bases and
integrations it is handed by id, with their stored credentials, code and
content. The ids a request sends are either already known - the user's
profile defaults, the agent's bindings, the General Assistant's settings - or
added by this request.

A known id is used as stored, as it always was: nothing is hidden or silently
ignored at chat time (maintainer decision). An
id the request ADDS must name a row its caller could bind themselves, the rule
of an agent's own bindings (``binding_access.refused_additions``: three-tier,
admins bypass): otherwise the request is refused before anything is stored
(:func:`refused_chat_selection`), with a sentence that names the kinds, never
an id, and reads the same for a row that does not exist. The ids the profile
makes known are checked where the profile is saved
(``PUT /settings/preferences``).

The conversation's saved selection (``chat_saved_tool_selection``) is not a
source of known ids: it lists what the agent was bound to when the message was
sent, and an id unbound since then is one the caller may not keep loading
unless they could bind it themselves. :func:`usable_saved_selection` is the
saved selection less those ids, read wherever it is used - the conversation
read the chat page restores its picker from, and the wake and channel turns
that run with it - so the page never sends one back and no turn loads one.
Such an id goes without a notice, as it goes from a new conversation with the
agent: the decision above keeps what is known, and this one is a row its owner
can no longer see, so there is nothing to name to them or to the model.

An integration keeps chat's own rule: the three-tier visibility with no admin
bypass (``integration_credentials.visible_integration_ids``, the list the chat
picker is built from). Whether the caller may use a visible integration's
personal OAuth token is not decided here: the turn's session resolver withholds
it and the turn says so, to the user and to the model
(``resolve_session_integration_bindings``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.services.chat_saved_tool_selection import saved_tool_selection

MCP_SERVERS = "mcp_server_ids"
CUSTOM_TOOLS = "custom_tool_ids"
KNOWLEDGE_BASES = "knowledge_base_ids"
INTEGRATIONS = "integration_ids"
SELECTION_KINDS = (MCP_SERVERS, CUSTOM_TOOLS, KNOWLEDGE_BASES, INTEGRATIONS)

_LABELS = {
    MCP_SERVERS: "MCP servers",
    CUSTOM_TOOLS: "custom tools",
    KNOWLEDGE_BASES: "knowledge bases",
    INTEGRATIONS: "integrations",
}


def _model(kind: str):
    if kind == MCP_SERVERS:
        from app.models.mcp_server import MCPServer

        return MCPServer
    if kind == CUSTOM_TOOLS:
        from app.models.custom_tool import CustomTool

        return CustomTool
    if kind == KNOWLEDGE_BASES:
        from app.models.knowledge_base import KnowledgeBase

        return KnowledgeBase
    from app.models.integration import Integration

    return Integration


def flat_ids(value: Any) -> list[str]:
    """Every id of a selection value: a list (nested lists included, as the
    chat client may send integrations) or a ``{provider: ids}`` binding map."""
    if isinstance(value, dict):
        value = list(value.values())
    out: list[str] = []
    stack = list(value) if isinstance(value, (list, tuple)) else []
    while stack:
        item = stack.pop(0)
        if isinstance(item, (list, tuple)):
            stack[:0] = list(item)
        elif item:
            out.append(str(item))
    return out


def _keys(value: Any) -> set[str]:
    return {str(parsed) for entry in flat_ids(value) if (parsed := safe_uuid(entry))}


def refusal_detail(kinds: Iterable[str]) -> str:
    """The error a request naming ids its caller cannot use gets. It names the
    kinds, never an id, and reads the same for a row that does not exist."""
    labels = [_LABELS[kind] for kind in SELECTION_KINDS if kind in set(kinds)]
    return (
        "The tool selection sent with this message names "
        f"{', '.join(labels)} you cannot use. Refresh the conversation's tool "
        "selection and send the message again."
    )


async def refused_chat_selection(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation: Any,
    requested: dict[str, Any],
) -> str | None:
    """The refusal of a new chat request whose selection ADDS an id its caller
    could not bind, or ``None``.

    Known ids - the caller's profile defaults, the agent's bindings or the
    General Assistant's settings - are never refused: they are used as stored.
    """
    if not any(requested.get(kind) for kind in SELECTION_KINDS):
        return None
    refused = await _refused_ids(db, user_id=user_id, conversation=conversation, requested=requested)
    kinds = [kind for kind in SELECTION_KINDS if refused[kind]]
    return refusal_detail(kinds) if kinds else None


async def usable_saved_selection(
    db: AsyncSession, *, user_id: Any, conversation: Any, lookups: dict | None = None
) -> dict[str, Any] | None:
    """The conversation's saved selection (``saved_tool_selection``), less the
    ids its owner may no longer load: neither bound to the agent, the General
    Assistant or the profile today, nor a row they could bind themselves - an
    MCP server unbound from the agent since the save, say. ``None`` without a
    selection saved for the conversation's agent.

    *lookups* is a dict a caller reading many conversations passes to each
    call, so their owners, groups, agents' bindings and what each owner may
    see are read once."""
    saved = saved_tool_selection(conversation)
    if saved is None or not any(flat_ids(saved.get(kind)) for kind in SELECTION_KINDS):
        return saved
    refused = await _refused_ids(
        db, user_id=safe_uuid(str(user_id)), conversation=conversation, requested=saved, lookups=lookups
    )
    if not any(refused.values()):
        return saved
    return {**saved, **{kind: _without(saved.get(kind), refused[kind]) for kind in SELECTION_KINDS if refused[kind]}}


def _without(value: Any, drop: set[str]) -> Any:
    """*value* (a list, nested lists or a ``{provider: ids}`` map) without the ids in *drop*."""
    if isinstance(value, dict):
        return {key: _without(ids, drop) for key, ids in value.items()}
    if isinstance(value, (list, tuple)):
        return [_without(item, drop) if isinstance(item, (list, tuple)) else item for item in value if str(item) not in drop]
    return value


async def _memo(lookups: dict | None, key: tuple, load):
    """``await load()``, read once per *key* across the calls sharing *lookups*."""
    if lookups is None:
        return await load()
    if key not in lookups:
        lookups[key] = await load()
    return lookups[key]


async def _refused_ids(
    db: AsyncSession,
    *,
    user_id: Any,
    conversation: Any,
    requested: dict[str, Any],
    lookups: dict | None = None,
) -> dict[str, set[str]]:
    """Per kind, the ids of *requested* its caller may not load: unknown
    (:func:`_known_ids`) and not a row they could bind. Only an unknown id
    costs the caller's groups and a visibility query."""
    from app.api.v1.binding_access import refused_additions
    from app.api.v1.helpers import get_user_group_ids
    from app.models.user import User

    async def _user():
        return (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()

    user = await _memo(lookups, ("user", user_id), _user)
    agent_id = getattr(conversation, "agent_id", None)
    known = await _memo(
        lookups,
        ("known", user_id, agent_id, None if agent_id else getattr(conversation, "channel", None)),
        lambda: _known_ids(db, user=user, conversation=conversation),
    )
    refused: dict[str, set[str]] = {kind: set() for kind in SELECTION_KINDS}
    unknown = {kind: [i for i in flat_ids(requested.get(kind)) if _keys([i]).isdisjoint(known[kind])] for kind in SELECTION_KINDS}
    if not any(unknown.values()):
        return refused
    caller = user if user is not None else SimpleNamespace(id=user_id, is_admin=False)
    group_ids = await _memo(lookups, ("groups", user_id), lambda: get_user_group_ids(db, user_id))
    for kind in SELECTION_KINDS:
        if not unknown[kind]:
            continue
        # id -> refused, per owner and kind: what they may see does not
        # depend on the conversation.
        verdicts = lookups.setdefault(("refused", user_id, kind), {}) if lookups is not None else {}
        pending = [i for i in unknown[kind] if i not in verdicts]
        if pending:
            denied = {
                str(entry)
                for entry in await refused_additions(
                    db,
                    user=caller,
                    model=_model(kind),
                    value=pending,
                    user_group_ids=group_ids,
                    is_admin=False if kind == INTEGRATIONS else None,
                )
            }
            verdicts.update({i: i in denied for i in pending})
        refused[kind] = {i for i in unknown[kind] if verdicts[i]}
    return refused


async def _known_ids(
    db: AsyncSession, *, user: Any, conversation: Any
) -> dict[str, set[str]]:
    known: dict[str, set[str]] = {kind: set() for kind in SELECTION_KINDS}

    if user is not None:
        known[MCP_SERVERS] |= _keys(user.default_mcp_server_ids) | _keys(user.ga_mcp_server_ids)
        known[KNOWLEDGE_BASES] |= _keys(user.default_knowledge_base_ids) | _keys(
            user.ga_knowledge_base_ids
        )
        known[CUSTOM_TOOLS] |= _keys(user.ga_custom_tool_ids)
        known[INTEGRATIONS] |= _keys(user.ga_integration_bindings)

    agent_id = getattr(conversation, "agent_id", None)
    agent = None
    if agent_id is not None:
        from app.models.agent import Agent

        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    elif user is not None:
        # The agent standing in for the General Assistant answers it:
        # the selection the chat sends is built from its bindings.
        from app.services.general_assistant_link import turn_stand_in

        agent = await turn_stand_in(
            db,
            agent_id=None,
            conversation_channel=getattr(conversation, "channel", None),
            user_id=user.id,
        )
    if agent is not None:
        known[MCP_SERVERS] |= _keys(agent.mcp_server_ids)
        known[CUSTOM_TOOLS] |= _keys(agent.custom_tool_ids)
        known[KNOWLEDGE_BASES] |= _keys(agent.knowledge_base_ids)
        known[INTEGRATIONS] |= _keys(agent.integration_bindings)
    elif agent_id is None:
        from app.models.general_assistant_config import GeneralAssistantConfig

        ga = (await db.execute(select(GeneralAssistantConfig).limit(1))).scalar_one_or_none()
        if ga is not None:
            known[MCP_SERVERS] |= _keys(ga.mcp_server_ids)
            known[CUSTOM_TOOLS] |= _keys(ga.custom_tool_ids)
            known[KNOWLEDGE_BASES] |= _keys(ga.knowledge_base_ids)
            known[INTEGRATIONS] |= _keys(ga.integration_bindings)
    return known
