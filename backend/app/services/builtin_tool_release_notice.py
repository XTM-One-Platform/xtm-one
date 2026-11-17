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

"""Tell the owners once per release which new built-in tools their agents' allowlists leave out (#12192).

``builtin_allowed_tools`` keeps least privilege: a function shipped in a
scoped group after the list was written is not granted. Nothing grants it
here either. This module is the signal to the people who can: at the first
boot of a build, the build's built-in catalog is stored on its
``platform_builds`` row (``builtin_tools``) and diffed against the previous
build's; every owner of an agent whose allowlist scopes a group that gained a
function gets ONE notification-bell entry naming the new functions and the
agents left without them, linking to the agent page.

"Once per release": only the process whose upsert inserted the build row
calls :func:`on_release_deployed` (``platform_builds.record_running_build``),
and only the call that writes the still-NULL snapshot sends; a racing second
writer finds it written and sends nothing. A previous build without a
snapshot (the first release that records one) sends nothing: there is no
baseline to diff. Never raises.

An owner who deliberately leaves a function out dismisses it (#12245): the
``group.function`` name is kept in the agent's ``config``
(:data:`DISMISSED_CONFIG_KEY`), the Tooling section stops listing it, a later
release never names it again for that agent, and the bell entries about the
agent are marked read once nothing they name is left to act on
(:func:`resolve_notices`). A function a later release ships is told as before.

A group whose list the platform wrote, not a person (#39: a former id folded
into its group, a group a fleet role granted scoped; the agent's ``config``
names it under ``IMPLICIT_SCOPE_CONFIG_KEY``), is no restriction anybody
chose: the release adds its new functions to the list itself
(:func:`grant_implicit`) and tells nobody.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

NOTIFICATION_SOURCE = "builtin_tools_outside_allowlist"
SOURCE_LABEL = "Built-in tools"
#: The agents read per release; an allowlist-scoped fleet larger than this is told about the first ones.
MAX_AGENTS = 5000
#: Agents and functions named in one summary before "and N more".
_SUMMARY_AGENTS = 10
_SUMMARY_FUNCTIONS = 12
#: ``Agent.config`` key of the ``group.function`` names the owner dismissed from the notice (#12245).
DISMISSED_CONFIG_KEY = "builtin_tools_dismissed"
#: Dismissed names kept per agent: past it the oldest go.
MAX_DISMISSED = 500
#: The person's newest notifications read to find the release notices about an agent.
_RESOLVE_WINDOW = 200


@dataclass
class AgentLeftOut:
    """One agent and, per scoped group, the new functions its allowlist leaves out."""

    agent_id: str
    name: str
    owner_id: str
    groups: dict[str, list[str]] = field(default_factory=dict)


def new_functions(
    previous: Mapping[str, Iterable[str]] | None, current: Mapping[str, Iterable[str]]
) -> dict[str, list[str]]:
    """Per group, the functions *current* ships that *previous* did not, in catalog order. Pure.

    ``{}`` when *previous* is ``None`` (no baseline). A group new in *current*
    is not listed: no allowlist can scope a group that did not exist.
    """
    if previous is None:
        return {}
    added: dict[str, list[str]] = {}
    for group, names in current.items():
        if group not in previous:
            continue
        before = set(previous[group])
        fresh = [name for name in names if name not in before]
        if fresh:
            added[group] = fresh
    return added


def dismissed_functions(config: Any) -> list[str]:
    """The ``group.function`` names dismissed on an agent's *config*, oldest first. Pure."""
    raw = config.get(DISMISSED_CONFIG_KEY) if isinstance(config, Mapping) else None
    if not isinstance(raw, list):
        return []
    return list(dict.fromkeys(name for name in raw if isinstance(name, str)))


def with_dismissed(config: Any, names: Iterable[str]) -> dict[str, Any]:
    """A copy of *config* with *names* dismissed, the newest :data:`MAX_DISMISSED` kept. Pure."""
    merged = dismissed_functions(config)
    merged += [name for name in dict.fromkeys(names) if name not in merged]
    return {**(dict(config) if isinstance(config, Mapping) else {}), DISMISSED_CONFIG_KEY: merged[-MAX_DISMISSED:]}


def still_outside(agent: Any, groups: Mapping[str, Iterable[str]]) -> dict[str, list[str]]:
    """Per group, the functions of *groups* the agent's allowlist leaves out and its owner did not dismiss. Pure.

    *agent* carries ``builtin_tool_ids``, ``builtin_allowed_tools`` and
    ``config``. A group the agent does not carry, or carries unscoped, has
    nothing left out.
    """
    return _left_out(agent, groups, implicit=False)


def implicit_left_out(agent: Any, groups: Mapping[str, Iterable[str]]) -> dict[str, list[str]]:
    """Per group, the functions of *groups* a platform-written list of the agent leaves out (#39). Pure."""
    return _left_out(agent, groups, implicit=True)


def _left_out(agent: Any, groups: Mapping[str, Iterable[str]], *, implicit: bool) -> dict[str, list[str]]:
    allowed = getattr(agent, "builtin_allowed_tools", None)
    if not isinstance(allowed, dict) or not allowed:
        return {}
    from app.services.builtin_allowed_tools import implicit_scope_groups
    from app.services.builtin_tool_aliases import canonical_builtin_selection, current_function_name

    try:
        ids, folded = canonical_builtin_selection(getattr(agent, "builtin_tool_ids", None) or (), allowed)
    except Exception as exc:  # a stale stored value never fails the notice
        logger.debug("Agent %s skipped by the release notice: %s", getattr(agent, "id", None), exc)
        return {}
    config = getattr(agent, "config", None)
    platform_written = set(implicit_scope_groups(config))
    dismissed = set(dismissed_functions(config))
    out: dict[str, list[str]] = {}
    for group, fresh in groups.items():
        names = (folded or {}).get(group)
        if group not in ids or names is None or (group in platform_written) != implicit:
            continue
        granted = {current_function_name(str(name)) for name in names}
        missing = [fn for fn in fresh if fn not in granted and f"{group}.{fn}" not in dismissed]
        if missing:
            out[group] = missing
    return out


def agents_left_out(added: Mapping[str, list[str]], agents: Iterable[Any]) -> list[AgentLeftOut]:
    """The agents whose allowlist scopes a group of *added* and leaves a new function out. Pure.

    *agents* carry ``id``, ``name``, ``user_id``, ``builtin_tool_ids``,
    ``builtin_allowed_tools`` and ``config`` (:func:`still_outside`). An agent
    without an owner is skipped; so is a function its owner dismissed.
    """
    if not added:
        return []
    out: list[AgentLeftOut] = []
    for agent in agents:
        owner = getattr(agent, "user_id", None)
        if not owner:
            continue
        groups = still_outside(agent, added)
        if groups:
            out.append(AgentLeftOut(str(agent.id), str(getattr(agent, "name", "") or agent.id), str(owner), groups))
    return out


def notice_resolved(extra: Mapping[str, Any], agents: Mapping[str, Any]) -> bool:
    """Whether a notice's ``extra_data`` names nothing left to act on. Pure.

    *agents* maps an agent id to its row (:func:`still_outside`); an agent
    gone from it is resolved, so is one that added or dismissed every
    function the notice named.
    """
    for entry in extra.get("agents") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("outside"), Mapping):
            continue
        agent = agents.get(str(entry.get("agent_id")))
        if agent is None:
            continue
        groups = {str(g): [str(f) for f in fns] for g, fns in entry["outside"].items() if isinstance(fns, list)}
        if still_outside(agent, groups):
            return False
    return True


def _names_agent(extra: Any, agent_id: str) -> bool:
    if not isinstance(extra, Mapping) or extra.get("notification_source") != NOTIFICATION_SOURCE:
        return False
    return any(isinstance(a, Mapping) and str(a.get("agent_id")) == agent_id for a in extra.get("agents") or [])


async def resolve_notices(db: Any, user_id: uuid.UUID, agent_id: str) -> list[str]:
    """Mark read the person's release notices about *agent_id* left with nothing to act on; their ids.

    Read state is the bell's own (``notification_feed``): a resolved notice is
    dismissed there, so it reads as read on every device. The caller commits.
    """
    from sqlalchemy import select

    from app.models.activity import Activity
    from app.models.agent import Agent
    from app.services.notification_feed import load_read_state, save_read_state

    rows = await db.execute(
        select(Activity.id, Activity.extra_data)
        .where(Activity.user_id == user_id, Activity.activity_type == "notification")
        .order_by(Activity.created_at.desc())
        .limit(_RESOLVE_WINDOW)
    )
    notices = [(str(row.id), row.extra_data) for row in rows.all() if _names_agent(row.extra_data, agent_id)]
    if not notices:
        return []
    agent_ids: set[uuid.UUID] = set()
    for _id, extra in notices:
        for entry in extra.get("agents") or []:
            try:
                agent_ids.add(uuid.UUID(str(entry.get("agent_id"))))
            except (AttributeError, TypeError, ValueError):
                continue
    found = await db.execute(
        select(Agent.id, Agent.builtin_tool_ids, Agent.builtin_allowed_tools, Agent.config).where(
            Agent.id.in_(agent_ids)
        )
    )
    agents = {str(row.id): row for row in found.all()}
    resolved = [notice_id for notice_id, extra in notices if notice_resolved(extra, agents)]
    if resolved:
        read_at, dismissed = await load_read_state(db, user_id)
        fresh = [notice_id for notice_id in resolved if notice_id not in dismissed]
        if fresh:
            await save_read_state(db, user_id, read_at=read_at, dismissed=[*dismissed, *fresh])
    return resolved


def by_owner(left_out: Iterable[AgentLeftOut]) -> dict[str, list[AgentLeftOut]]:
    """*left_out* grouped by owner, each owner's agents in name order. Pure."""
    grouped: dict[str, list[AgentLeftOut]] = {}
    for entry in left_out:
        grouped.setdefault(entry.owner_id, []).append(entry)
    for entries in grouped.values():
        entries.sort(key=lambda e: (e.name.lower(), e.agent_id))
    return grouped


def _more(items: list[str], limit: int) -> str:
    shown = ", ".join(items[:limit])
    return f"{shown} and {len(items) - limit} more" if len(items) > limit else shown


#: The longest path a notification opens (``app.core.internal_paths``).
_MAX_PATH = 300


def tooling_path(agent: AgentLeftOut) -> str:
    """The agent's Tooling section, naming in ``added`` the new functions it leaves out. Pure.

    The page offers a one-click add for each one still outside the allowlist.
    Names that would push the path past :data:`_MAX_PATH` are dropped: the
    page still opens on the section.
    """
    base = f"/agents/{agent.agent_id}/config/tooling"
    names: list[str] = []
    for group, fns in sorted(agent.groups.items()):
        for fn in fns:
            candidate = [*names, f"{group}.{fn}"]
            if len(f"{base}?added={','.join(candidate)}") > _MAX_PATH:
                return f"{base}?added={','.join(names)}" if names else base
            names = candidate
    return f"{base}?added={','.join(names)}" if names else base


def notice(release: str, agents: list[AgentLeftOut]) -> tuple[str, str, dict[str, Any]]:
    """``(title, summary, extra_data)`` of one owner's notification for *release*. Pure."""
    functions = sorted({f"{group}.{fn}" for a in agents for group, fns in a.groups.items() for fn in fns})
    count = len(functions)
    noun = "tool is" if count == 1 else "tools are"
    title = f"{count} new built-in {noun} outside the allowlist of {len(agents)} of your agents"
    if len(agents) == 1:
        title = f"{count} new built-in {noun} outside the allowlist of agent '{agents[0].name}'"
    names = [f"'{a.name}'" for a in agents]
    tools = "it" if count == 1 else "them"
    if len(agents) == 1:
        who = f"Agent {names[0]} is limited to some tools of this group, so it does not get {tools}. "
        add = f"Add {tools} from the agent page if it needs {tools}. "
    else:
        who = f"Agents {_more(names, _SUMMARY_AGENTS)} are limited to some tools of these groups, so they do not get {tools}. "
        add = f"Add {tools} from each agent page if the agent needs {tools}. "
    summary = f"Release {release} ships {_more(functions, _SUMMARY_FUNCTIONS)}. {who}{add}This notice is sent once per release."
    extra = {
        "notification_source": NOTIFICATION_SOURCE,
        "release": release,
        "agents": [{"agent_id": a.agent_id, "name": a.name, "outside": a.groups} for a in agents],
        "path": tooling_path(agents[0]),
    }
    return title, summary, extra


def _catalog() -> dict[str, list[str]]:
    from app.tools.builtin_suites import builtin_suite_catalog

    return {group: list(names) for group, names in builtin_suite_catalog().items()}


async def _store_snapshot(db: Any, version: str, git_sha: str, catalog: dict[str, list[str]]) -> bool:
    """Write *catalog* on the build row while it is still NULL; whether this call wrote it."""
    from sqlalchemy import update

    from app.models.platform_build import PlatformBuild

    result = await db.execute(
        update(PlatformBuild)
        .where(
            PlatformBuild.version == version,
            PlatformBuild.git_sha == git_sha,
            PlatformBuild.builtin_tools.is_(None),
        )
        .values(builtin_tools=catalog)
        .returning(PlatformBuild.id)
    )
    return result.first() is not None


async def _previous_snapshot(db: Any, version: str | None, git_sha: str | None) -> dict[str, Any] | None:
    if not version:
        return None
    from sqlalchemy import select

    from app.models.platform_build import PlatformBuild

    value = await db.scalar(
        select(PlatformBuild.builtin_tools)
        .where(PlatformBuild.version == version, PlatformBuild.git_sha == (git_sha or ""))
        .limit(1)
    )
    return value if isinstance(value, dict) else None


def granted_allowlist(agent: Any, groups: Mapping[str, Iterable[str]]) -> dict[str, list[str]] | None:
    """The agent's ``builtin_allowed_tools`` with *groups*' functions added to their lists, canonical. Pure."""
    from app.services.builtin_tool_aliases import canonical_builtin_selection

    _ids, folded = canonical_builtin_selection(
        getattr(agent, "builtin_tool_ids", None) or (), getattr(agent, "builtin_allowed_tools", None)
    )
    out = {g: list(fns) for g, fns in (folded or {}).items()}
    for group, fns in groups.items():
        if out.get(group) is not None:
            out[group] += [fn for fn in fns if fn not in out[group]]
    return out or None


async def grant_implicit(db: Any, added: Mapping[str, list[str]], agents: Iterable[Any]) -> int:
    """Add *added*'s functions to every platform-written list that leaves them out (#39); the agents changed.

    Only ``builtin_allowed_tools`` is written; ``builtin_tool_ids`` and the
    marker stay as they are. The caller commits.
    """
    if not added:
        return 0
    from sqlalchemy import update

    from app.models.agent import Agent

    changed = 0
    for agent in agents:
        groups = implicit_left_out(agent, added)
        if not groups:
            continue
        await db.execute(
            update(Agent).where(Agent.id == agent.id).values(builtin_allowed_tools=granted_allowlist(agent, groups))
        )
        logger.info(
            "Agent %s: new built-in tools %s granted (allowlist written by the platform).",
            agent.id,
            ", ".join(f"{g}.{fn}" for g, fns in sorted(groups.items()) for fn in fns),
        )
        changed += 1
    return changed


async def _scoped_agents(db: Any) -> list[Any]:
    from sqlalchemy import select

    from app.models.agent import Agent

    rows = await db.execute(
        select(Agent.id, Agent.name, Agent.user_id, Agent.builtin_tool_ids, Agent.builtin_allowed_tools, Agent.config)
        .where(Agent.builtin_allowed_tools.is_not(None))
        .limit(MAX_AGENTS)
    )
    return list(rows.all())


_tasks: set[asyncio.Task[int]] = set()


def schedule_release_notice(release: dict[str, Any]) -> None:
    """Run :func:`on_release_deployed` in the background (a startup step must not wait on the catalog)."""
    coro = on_release_deployed(dict(release))
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # no loop: nothing to schedule on
        coro.close()
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def on_release_deployed(release: dict[str, Any]) -> int:
    """Snapshot this build's built-in catalog and notify the owners; the owners notified. Never raises."""
    try:
        from app.core.database import async_session
        from app.services.notify import notify_web_user

        version = str(release.get("version") or "")
        git_sha = str(release.get("git_sha") or "")
        catalog = await asyncio.to_thread(_catalog)
        if not version or not catalog:
            return 0
        async with async_session() as db:
            if not await _store_snapshot(db, version, git_sha, catalog):
                await db.rollback()
                return 0
            previous = await _previous_snapshot(db, release.get("previous_version"), release.get("previous_git_sha"))
            added = new_functions(previous, catalog)
            scoped = await _scoped_agents(db) if added else []
            granted = await grant_implicit(db, added, scoped)
            owners = by_owner(agents_left_out(added, scoped)) if added else {}
            label = str(release.get("release") or version)
            for owner_id, agents in owners.items():
                title, summary, extra = notice(label, agents)
                await notify_web_user(
                    db, owner_id, title=title, summary=summary, source_label=SOURCE_LABEL, extra_data=extra
                )
            await db.commit()
        if granted:
            logger.info("Release %s: new built-in tools granted to %d agent(s) scoped by the platform.", label, granted)
        if owners:
            logger.info("Release %s: new built-in tools outside allowlists, %d owner(s) notified.", label, len(owners))
        return len(owners)
    except Exception as exc:
        logger.warning("Built-in tool release notice not sent: %s", format_exception_message(exc))
        return 0
