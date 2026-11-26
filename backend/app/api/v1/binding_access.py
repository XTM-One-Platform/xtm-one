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

"""Who may ADD a binding to an agent or an assignment.

Several stored lists name resources that a run later uses with their stored
credentials:

- an assignment's ``tool_sources`` (MCP servers; ``load_mcp_tools`` loads every
  listed server that is enabled);
- an assignment's ``integration_bindings`` override (it replaces the agent's
  bindings, ``assignment_engine``);
- the channel bots named by an agent's ``channel_config`` and by an
  assignment's ``channel_config`` override (``build_channel_tools``);
- the integration an assignment's trigger names (``trigger_config.integration_id``,
  whose events the event poller reads with its credentials). Unlike the
  lists above it must be one the writer can see, whatever the agent binds; the
  delivery only warns, so this check is where it is enforced;
- the agents an agent's work-item roster names (``work_item_owner_agent_ids``),
  which it may hand work to: an added one must be an agent the writer can see.
- the agents an agent's trace supervision allow-list names
  (``trace_supervision_target_agent_ids``), whose traces it may inspect: an
  added one must be an agent whose conversations the writer may read, a
  narrower rule than seeing it (the trace tools check it again on every read).
- the relay group an integration, an MCP server or a flow node routes through
  (``relay_group_id``): its requests leave from that group's network,
  so a newly picked group must be one the writer can see.

A run uses what these lists name as stored, with no visibility check of its
own, so each list is checked where it is written, with the rules the agent's
own bindings follow (``validate_personal_agent_bindings`` /
``validate_company_managed_bindings`` in ``services/agent_bindings.py``). An entry a write ADDS
is allowed when it is:

- one of the parent agent's own bindings - the agent already runs with it;
- on a company-managed agent, a company-managed resource where the agent's own
  bindings require one (MCP servers, integrations);
- otherwise a resource the writer can see (three-tier; admins bypass, as they
  do for the agent's bindings).

Entries the row already holds are never re-checked, so a row that carries a
stale id (a resource unshared since, an older import) keeps saving. A refusal
is one 400 whose sentence is the same whether the resource is missing or only
invisible to the writer.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.agent_import import UNRESOLVED_TOOL_SOURCE
from app.api.v1.helpers import (
    bindable_knowledge_base_clause,
    get_user_group_ids,
    user_visible_filter,
)
from app.core.utils import safe_uuid
from app.models.agent import Agent
from app.models.channel_bot import ChannelBot
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.relay import RelayGroup
from app.models.user import User

TOOL_SOURCES_REFUSED_DETAIL = (
    "One or more MCP servers in tool_sources were not found or are not accessible."
)
INTEGRATIONS_REFUSED_DETAIL = (
    "One or more integrations in integration_bindings were not found or are not "
    "accessible."
)
CHANNEL_BOTS_REFUSED_DETAIL = (
    "One or more channel bots in channel_config were not found or are not accessible."
)
TRIGGER_INTEGRATION_REFUSED_DETAIL = (
    "The integration in trigger_config was not found or is not accessible."
)
WORK_ITEM_OWNERS_REFUSED_DETAIL = (
    "One or more work-item owner agents were not found or are not accessible."
)
TRACE_SUPERVISION_TARGETS_REFUSED_DETAIL = (
    "One or more trace supervision target agents were not found, or you may not "
    "read their conversations."
)
WHITELIST_BYPASS_REFUSED_DETAIL = "Only admins can enable bypass_whitelist on a channel."
RELAY_GROUP_REFUSED_DETAIL = "The relay group was not found or is not accessible."


def _key(entry: object) -> str:
    parsed = entry if isinstance(entry, uuid.UUID) else safe_uuid(str(entry))
    return str(parsed) if parsed is not None else f"raw:{entry!r}"


def integration_binding_ids(bindings: dict | None) -> list:
    """Every integration id of a ``{provider: [ids]}`` binding map."""
    if not isinstance(bindings, dict):
        return []
    return [
        iid
        for ids in bindings.values()
        for iid in (ids if isinstance(ids, list) else [ids])
        if iid
    ]


def channel_bot_ids(channel_config: dict | None) -> list:
    """Every channel bot id a ``{channel_type: {...}}`` config names."""
    if not isinstance(channel_config, dict):
        return []
    return [
        cfg["channel_bot_id"]
        for cfg in channel_config.values()
        if isinstance(cfg, dict) and cfg.get("channel_bot_id")
    ]


async def ids_matching(db: AsyncSession, model, raw_ids, clause) -> set[str]:
    """The ids among ``raw_ids`` (any spelling) of ``model`` rows matching ``clause``."""
    uuids = {parsed for raw in raw_ids or [] if (parsed := safe_uuid(str(raw)) if raw else None)}
    if not uuids:
        return set()
    result = await db.execute(select(model.id).where(model.id.in_(uuids), clause))
    return {str(row[0]) for row in result.all()}


async def refused_additions(
    db: AsyncSession,
    *,
    user: User,
    model,
    value: list | None,
    baseline: list | None = None,
    agent_bound: list | None = None,
    company_only: bool = False,
    user_group_ids: list[str] | None = None,
    is_admin: bool | None = None,
) -> list:
    """The entries of ``value`` absent from ``baseline`` and ``agent_bound``
    that ``user`` may not add: company-managed rows only when ``company_only``,
    otherwise the rows ``user`` can see. An entry naming no row is refused
    like a hidden one.

    ``is_admin`` is the admin bypass on that visibility, ``user.is_admin`` by
    default; an agent tool passes ``agent_tool_admin_bypass(user)``,
    and chat passes ``False`` for integrations, whose chat rule has none."""
    if not value:
        return []
    known = {_key(entry) for entry in baseline or []} | {
        _key(entry) for entry in agent_bound or []
    }
    refused: list = []
    candidates: dict[str, list] = {}
    for entry in value:
        key = _key(entry)
        if key in known:
            continue
        if key.startswith("raw:"):
            refused.append(entry)
        else:
            candidates.setdefault(key, []).append(entry)
    if not candidates:
        return refused

    if company_only:
        # access_restricted: bypass - only admins write a company-managed agent's rows, and an admin may bind any company-managed row.
        clause = model.is_company_managed.is_(True)
    else:
        if user_group_ids is None:
            user_group_ids = await get_user_group_ids(db, user.id)
        clause = user_visible_filter(
            model,
            user.id,
            user_group_ids,
            is_admin=bool(user.is_admin if is_admin is None else is_admin),
        )
    if model is KnowledgeBase:
        # Never a workspace's own or an internal knowledge base.
        clause = and_(clause, bindable_knowledge_base_clause())
    allowed = await ids_matching(db, model, list(candidates), clause)
    for key, entries in candidates.items():
        if key not in allowed:
            refused.extend(entries)
    return refused


# --- Assignment MCP scope (``tool_sources``) --------------------------------


async def refused_tool_source_additions(
    db: AsyncSession,
    *,
    user: User,
    agent: Agent,
    value: list | None,
    baseline: list | None,
    user_group_ids: list[str] | None = None,
) -> list:
    """The entries of ``value`` absent from ``baseline`` that ``user`` may not add."""
    if not value:
        return []
    return await refused_additions(
        db,
        user=user,
        model=MCPServer,
        value=value,
        baseline=baseline,
        agent_bound=agent.mcp_server_ids,
        company_only=agent.is_company_managed,
        user_group_ids=user_group_ids,
    )


async def assert_tool_source_additions_allowed(
    db: AsyncSession,
    *,
    user: User,
    agent: Agent,
    value: list | None,
    baseline: list | None,
    user_group_ids: list[str] | None = None,
) -> None:
    """Refuse (400) a write that adds a server ``user`` may not use."""
    if await refused_tool_source_additions(
        db,
        user=user,
        agent=agent,
        value=value,
        baseline=baseline,
        user_group_ids=user_group_ids,
    ):
        raise HTTPException(status_code=400, detail=TOOL_SOURCES_REFUSED_DETAIL)


async def tool_sources_for_copy(
    db: AsyncSession,
    *,
    user: User,
    agent: Agent,
    value: list | None,
    user_group_ids: list[str] | None = None,
) -> tuple[list | None, int]:
    """``value`` as a copy made by ``user`` onto ``agent`` may carry it, and
    how many servers it names that the copy leaves out.

    A copy is a new row, so every entry is an addition: the ones ``user`` may
    not add are dropped. A non-empty scope with nothing left becomes
    :data:`UNRESOLVED_TOOL_SOURCE` - an empty list would mean every server of
    the agent to the assignment engine. That marker names no server, so a
    source scope carrying it is not counted as losing one.
    """
    if not isinstance(value, list) or not value:
        return value, 0
    refused = await refused_tool_source_additions(
        db,
        user=user,
        agent=agent,
        value=value,
        baseline=None,
        user_group_ids=user_group_ids,
    )
    if not refused:
        return list(value), 0
    refused_keys = {_key(entry) for entry in refused}
    kept = [entry for entry in value if _key(entry) not in refused_keys]
    return (
        kept or [UNRESOLVED_TOOL_SOURCE],
        len(refused_keys - {UNRESOLVED_TOOL_SOURCE}),
    )


async def copyable_tool_sources(
    db: AsyncSession,
    *,
    user: User,
    agent: Agent,
    value: list | None,
    user_group_ids: list[str] | None = None,
) -> list | None:
    """``value`` as a copy made by ``user`` onto ``agent`` may carry it
    (:func:`tool_sources_for_copy` without the count)."""
    scope, _dropped = await tool_sources_for_copy(
        db, user=user, agent=agent, value=value, user_group_ids=user_group_ids
    )
    return scope


# --- Assignment integration override ----------------------------------------


async def assert_integration_additions_allowed(
    db: AsyncSession,
    *,
    user: User,
    agent: Agent,
    value: dict | None,
    baseline: dict | None,
    user_group_ids: list[str] | None = None,
) -> None:
    """Refuse (400) an assignment write that adds an integration ``user`` may
    not bind to ``agent`` (company-managed agents: company-managed only)."""
    if not value:
        return
    if await refused_additions(
        db,
        user=user,
        model=Integration,
        value=integration_binding_ids(value),
        baseline=integration_binding_ids(baseline),
        agent_bound=integration_binding_ids(agent.integration_bindings),
        company_only=agent.is_company_managed,
        user_group_ids=user_group_ids,
    ):
        raise HTTPException(status_code=400, detail=INTEGRATIONS_REFUSED_DETAIL)


# --- Assignment trigger integration ---------------------------------


def trigger_integration_ref(trigger_config: object) -> object | None:
    """The integration an assignment's ``trigger_config`` names, as stored;
    ``None`` when it names none (absent, null or a blank string)."""
    if not isinstance(trigger_config, dict):
        return None
    value = trigger_config.get("integration_id")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


def trigger_integration_unchanged(trigger_config: object, baseline: object) -> bool:
    """Whether ``trigger_config`` names the integration ``baseline`` already
    stores, compared canonically (any UUID spelling), so it is not judged."""
    stored = trigger_integration_ref(baseline)
    new = trigger_integration_ref(trigger_config)
    return stored is not None and new is not None and _key(stored) == _key(new)


async def trigger_integration_refused(
    db: AsyncSession,
    *,
    user: User,
    value: object,
    baseline: object = None,
    user_group_ids: list[str] | None = None,
    is_admin: bool | None = None,
) -> bool:
    """Whether ``value`` (a ``trigger_config``) names an integration that
    ``baseline`` (the stored one) did not and that ``user`` cannot see.

    The trigger's integration is read for its events whatever the agent binds,
    so it must be visible to its writer (three-tier; admins see all, with the
    ``is_admin`` bypass of :func:`refused_additions`). An id naming no
    integration is refused like a hidden one; the id already stored, in any
    spelling, is never re-judged.
    """
    new = trigger_integration_ref(value)
    if new is None:
        return False
    stored = trigger_integration_ref(baseline)
    return bool(
        await refused_additions(
            db,
            user=user,
            model=Integration,
            value=[new],
            baseline=[stored] if stored is not None else None,
            user_group_ids=user_group_ids,
            is_admin=is_admin,
        )
    )


async def assert_trigger_integration_allowed(
    db: AsyncSession,
    *,
    user: User,
    value: object,
    baseline: object = None,
    user_group_ids: list[str] | None = None,
    is_admin: bool | None = None,
) -> None:
    """Refuse (400) a write whose trigger names a new integration ``user``
    cannot see (:func:`trigger_integration_refused`)."""
    if await trigger_integration_refused(
        db,
        user=user,
        value=value,
        baseline=baseline,
        user_group_ids=user_group_ids,
        is_admin=is_admin,
    ):
        raise HTTPException(status_code=400, detail=TRIGGER_INTEGRATION_REFUSED_DETAIL)


def without_trigger_integration(trigger_config: dict) -> dict:
    """``trigger_config`` minus the integration it names: the trigger then
    reads the agent's own integration of its provider, as one that names none."""
    return {key: value for key, value in trigger_config.items() if key != "integration_id"}


async def trigger_config_for_copy(
    db: AsyncSession,
    *,
    user: User,
    trigger_config: object,
    user_group_ids: list[str] | None = None,
    is_admin: bool | None = None,
) -> tuple[object, bool]:
    """``trigger_config`` as a trigger ``user`` creates from it may carry it,
    and whether the integration it names was left out.

    A copied trigger (a duplicate, a template's default assignment) is a new
    one, so its integration must be one ``user`` can see
    (:func:`trigger_integration_refused`); otherwise it is removed with
    :func:`without_trigger_integration`, never refused.
    """
    if isinstance(trigger_config, dict) and await trigger_integration_refused(
        db,
        user=user,
        value=trigger_config,
        user_group_ids=user_group_ids,
        is_admin=is_admin,
    ):
        return without_trigger_integration(trigger_config), True
    return trigger_config, False


# --- Agent work-item owner roster -------------------------------------------


async def assert_work_item_owner_additions_allowed(
    db: AsyncSession,
    *,
    user: User,
    value: list | None,
    baseline: list | None = None,
    user_group_ids: list[str] | None = None,
) -> None:
    """Refuse (400) a write that adds to an agent's ``work_item_owner_agent_ids``
    an agent ``user`` cannot see (three-tier; admins see all).

    The roster names the agents this agent may hand work items to, like a
    handover target names one it delegates to. Entries already stored are
    never re-checked, so re-saving a roster a colleague extended keeps saving.
    """
    if value and await refused_additions(
        db,
        user=user,
        model=Agent,
        value=list(value),
        baseline=list(baseline or []),
        user_group_ids=user_group_ids,
    ):
        raise HTTPException(status_code=400, detail=WORK_ITEM_OWNERS_REFUSED_DETAIL)


# --- Agent trace supervision allow-list -------------------------------------


async def assert_trace_supervision_additions_allowed(
    db: AsyncSession,
    *,
    user: User,
    value: list | None,
    baseline: list | None = None,
    user_group_ids: list[str] | None = None,
) -> None:
    """Refuse (400) a write that adds to an agent's
    ``trace_supervision_target_agent_ids`` an agent whose traces ``user`` may
    not read (``traces.agents_whose_traces_user_may_read``: an admin, its owner,
    a ``config_with_chats`` grantee). A target's traces hold every user's
    conversations with it, so naming one is reading them. An entry naming no
    agent is refused alike; entries already stored are never re-checked.
    """
    from app.services.builtin_tools.traces import agents_whose_traces_user_may_read

    known = {_key(entry) for entry in baseline or []}
    added = {_key(entry) for entry in value or [] if entry} - known
    if not added:
        return
    readable = await agents_whose_traces_user_may_read(
        db, user, [key for key in added if not key.startswith("raw:")], user_group_ids
    )
    if added - readable:
        raise HTTPException(status_code=400, detail=TRACE_SUPERVISION_TARGETS_REFUSED_DETAIL)


# --- Channel bots (agent and assignment ``channel_config``) -----------------


def added_channel_entries(
    channel_config: dict | None,
    *,
    baseline: dict | None = None,
    agent_bound: list | None = None,
) -> dict:
    """The entries of ``channel_config`` naming a bot neither ``baseline`` nor
    ``agent_bound`` already names. An entry naming no bot binds nothing."""
    if not isinstance(channel_config, dict):
        return {}
    known = {_key(bot) for bot in channel_bot_ids(baseline)} | {
        _key(bot) for bot in agent_bound or []
    }
    return {
        ch_type: cfg
        for ch_type, cfg in channel_config.items()
        if isinstance(cfg, dict)
        and cfg.get("channel_bot_id")
        and _key(cfg["channel_bot_id"]) not in known
    }


async def refused_channel_bots(
    db: AsyncSession,
    *,
    user: User,
    channel_config: dict | None,
    user_group_ids: list[str] | None = None,
    is_admin: bool | None = None,
) -> set[str]:
    """The (canonical) bot ids of ``channel_config`` that ``user`` cannot see."""
    refused = await refused_additions(
        db,
        user=user,
        model=ChannelBot,
        value=channel_bot_ids(channel_config),
        user_group_ids=user_group_ids,
        is_admin=is_admin,
    )
    return {_key(bot) for bot in refused}


async def assert_channel_bot_additions_allowed(
    db: AsyncSession,
    *,
    user: User,
    channel_config: dict | None,
    baseline: dict | None = None,
    agent_bound: list | None = None,
    user_group_ids: list[str] | None = None,
    is_admin: bool | None = None,
) -> dict:
    """Refuse (400) a write whose ``channel_config`` adds a bot ``user`` cannot
    see; return the added entries (for the caller's own entry checks)."""
    added = added_channel_entries(
        channel_config, baseline=baseline, agent_bound=agent_bound
    )
    if added and await refused_channel_bots(
        db,
        user=user,
        channel_config=added,
        user_group_ids=user_group_ids,
        is_admin=is_admin,
    ):
        raise HTTPException(status_code=400, detail=CHANNEL_BOTS_REFUSED_DETAIL)
    return added


def _bypasses_whitelist(entry: object) -> bool:
    return isinstance(entry, dict) and bool(entry.get("bypass_whitelist"))


def assert_whitelist_bypass_not_added(
    channel_config: dict | None,
    *,
    user: User,
    baseline: dict | None = None,
    agent_config: dict | None = None,
) -> None:
    """Refuse (403) a non-admin write that turns ``bypass_whitelist`` on.

    The flag lets a run publish on every channel of the bot, past the bot's
    whitelist (``build_channel_tools``), and only admins may set it
    (``validate_channel_config``). An entry naming a bot the row or the agent
    already names skips that check, since it binds nothing new, so the flag is
    checked on its own: a non-admin may keep a bypass that the stored entry or
    the agent's own entry for that channel and bot already carries (a re-save,
    the agent's settings copied into the override), never turn one on.
    """
    if getattr(user, "is_admin", False) or not isinstance(channel_config, dict):
        return
    stored = baseline if isinstance(baseline, dict) else {}
    inherited = agent_config if isinstance(agent_config, dict) else {}
    for ch_type, cfg in channel_config.items():
        if not _bypasses_whitelist(cfg):
            continue
        bot = _key(cfg.get("channel_bot_id"))
        if not any(
            _bypasses_whitelist(existing) and _key(existing.get("channel_bot_id")) == bot
            for existing in (stored.get(ch_type), inherited.get(ch_type))
        ):
            raise HTTPException(status_code=403, detail=WHITELIST_BYPASS_REFUSED_DETAIL)


def channel_bot_key(bot_id: object) -> str:
    """The canonical form bot ids are compared in (see :func:`refused_channel_bots`)."""
    return _key(bot_id)


# --- Relay group routing (``relay_group_id``) -------------------------


async def relay_group_refused(
    db: AsyncSession,
    *,
    user: User,
    value: object,
    baseline: object = None,
    is_admin: bool | None = None,
) -> bool:
    """Whether ``user`` may not route a resource through the relay group ``value``.

    Routing a resource through a group sends its requests - credentials
    included - from that group's network, so a writer may only pick a group
    they can see (three-tier, admins bypass). ``None`` (send directly) is never
    refused, and the group the row already names is never re-judged.
    """
    if value is None or value == "":
        return False
    return bool(
        await refused_additions(
            db,
            user=user,
            model=RelayGroup,
            value=[value],
            baseline=[baseline] if baseline else None,
            is_admin=is_admin,
        )
    )


async def assert_relay_group_binding_allowed(
    db: AsyncSession,
    *,
    user: User,
    value: object,
    baseline: object = None,
    is_admin: bool | None = None,
) -> None:
    """:func:`relay_group_refused` as a 400."""
    if await relay_group_refused(
        db, user=user, value=value, baseline=baseline, is_admin=is_admin
    ):
        raise HTTPException(status_code=400, detail=RELAY_GROUP_REFUSED_DETAIL)


async def checked_relay_group_id(
    db: AsyncSession,
    *,
    user: User,
    value: object,
    baseline: object = None,
    is_admin: bool | None = None,
) -> uuid.UUID | None:
    """The relay group id a write stores: ``None`` for "send directly", or a 400."""
    if value is None or value == "":
        return None
    await assert_relay_group_binding_allowed(
        db, user=user, value=value, baseline=baseline, is_admin=is_admin
    )
    return safe_uuid(str(value))
