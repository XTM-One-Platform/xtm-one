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

"""Intent bindings from chat: the agent-tool half of Intents.

An agent is offered in a connected platform's AI component (a playbook
step, an enrichment, a chat) only through an intent binding: the platform
reads ``GET /intents/catalog`` for its intent and vertical, and that
catalog lists the enabled, shared or company-managed agents bound to it.
Until these tools, the binding was a UI-only setting: an agent building
another agent could neither expose it nor see why it was missing.

* ``list_agent_intent_bindings`` - the bindings of one agent (intent,
  vertical, priority, locked, built-in, global or personal), and whether
  the agent can appear in the catalog at all;
* ``bind_agent_to_intent`` / ``unbind_agent_from_intent`` - the writes of
  ``PUT /intents/{id}/bind`` and ``DELETE /intents/{id}/bind/{binding_id}``,
  through the same writer (``services/intent_binding_writes.py``), so the
  gates are the API's: an administrator writes the global binding, anyone
  else a personal override; a private agent is never bound globally; a
  locked default is never overridden. Built-in bindings stay read-only here.

Every write is audited like the agent-builder writes, marked ``via: agent``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.core.utils import safe_uuid
from app.services.intent_binding_writes import WINNER_RULE
from app.tools.base import ToolDefinition

from . import agent_builder as _builder
from ._shared import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent
    from app.models.intent import Intent, IntentBinding
    from app.models.user import User
    from app.services.intent_binding_writes import IntentWinner

LIST_TOOL = "list_agent_intent_bindings"
BIND_TOOL = "bind_agent_to_intent"
UNBIND_TOOL = "unbind_agent_from_intent"
INTENT_BINDING_TOOL_NAMES = frozenset({LIST_TOOL, BIND_TOOL, UNBIND_TOOL})

_ANONYMOUS_REFUSAL = "Intent bindings are not available for anonymous users."


def binding_line(binding: IntentBinding, intent_name: str, viewer_id: uuid.UUID | None) -> str:
    """One binding on one line, as the list, the bind and get_agent show it."""
    from app.services.intent_binding_writes import binding_owner_label

    flags = [binding_owner_label(binding, viewer_id)]
    if binding.is_locked:
        flags.append("locked")
    if binding.is_builtin:
        flags.append("built-in")
    overrides = binding.priority_overrides or {}
    priority = f"priority {binding.priority}"
    if overrides:
        priority += " (" + ", ".join(f"{v}: {p}" for v, p in sorted(overrides.items())) + ")"
    return (
        f"`{intent_name}` - vertical {binding.vertical or 'all'} - {priority} - "
        f"{', '.join(flags)} (binding `{binding.id}`)"
    )


def catalog_eligibility(agent: Agent) -> str | None:
    """Why the catalog does not list *agent* whatever its bindings, or ``None``."""
    from app.services.intent_binding_writes import agent_is_shared

    reasons = []
    if not agent.enabled:
        reasons.append("it is disabled")
    if not agent_is_shared(agent):
        reasons.append("it is private (neither company-managed nor shared with a group)")
    return " and ".join(reasons) or None


async def agent_binding_lines(db: AsyncSession, agent: Agent, viewer_id: uuid.UUID | None) -> list[str]:
    """The binding lines of *agent* every caller who can see it may read:
    global bindings and the viewer's own overrides (another person's
    override is theirs)."""
    from sqlalchemy import ColumnElement, or_, select

    from app.models.intent import Intent, IntentBinding

    owner: ColumnElement[bool] = IntentBinding.user_id.is_(None)
    if viewer_id is not None:
        owner = or_(owner, IntentBinding.user_id == viewer_id)
    rows = (
        await db.execute(
            select(IntentBinding, Intent.name)
            .join(Intent, IntentBinding.intent_id == Intent.id)
            .where(IntentBinding.agent_id == agent.id, owner)
            .order_by(Intent.name, IntentBinding.vertical)
        )
    ).all()
    return [binding_line(b, name, viewer_id) for b, name in rows]


def winner_line(intent_name: str, vertical: str | None, winner: IntentWinner | None, agent_id: uuid.UUID) -> str:
    """Who the catalog serves first for *intent_name* in *vertical*, from *agent_id*'s side."""
    where = f"vertical {vertical}" if vertical else "no vertical filter"
    if winner is None:
        return f"`{intent_name}` ({where}): no agent listed, so none wins"
    who = f"**{winner.agent_name}** (`{winner.agent_id}`) at effective priority {winner.priority}"
    if winner.agent_id == agent_id:
        return f"`{intent_name}` ({where}): this agent wins, {who}"
    return (
        f"`{intent_name}` ({where}): {who} wins; this agent needs an effective priority of at least "
        f"{winner.priority + 1} to win there"
    )


async def agent_winner_lines(
    db: AsyncSession, user: User, agent: Agent, *, intent_id: uuid.UUID | None = None
) -> list[str]:
    """Per intent *agent* is bound to (or *intent_id* alone) and per vertical, the winner.

    The verticals are the ones the intent's bindings name, plus no vertical
    filter; the candidates are the catalog's for *user*
    (``intent_binding_writes.intent_winners``).
    """
    from sqlalchemy import select

    from app.models.intent import Intent, IntentBinding
    from app.services.intent_binding_writes import intent_verticals, intent_winners

    if intent_id is None:
        ids = (
            await db.execute(select(IntentBinding.intent_id).where(IntentBinding.agent_id == agent.id).distinct())
        ).scalars().all()
    else:
        ids = [intent_id]
    lines: list[str] = []
    for iid in ids:
        name = (await db.execute(select(Intent.name).where(Intent.id == iid))).scalar_one_or_none()
        bindings = (await db.execute(select(IntentBinding).where(IntentBinding.intent_id == iid))).scalars().all()
        winners = await intent_winners(db, user, iid, intent_verticals(bindings))
        lines.extend(winner_line(str(name or iid), v, w, agent.id) for v, w in winners.items())
    return sorted(lines)


async def source_intent_bindings(db: AsyncSession, agent_id: uuid.UUID) -> list[tuple[str, str | None, bool]]:
    """The global bindings of an agent a clone does not carry:
    (intent name, vertical, built-in), personal overrides left out (they are
    their owner's)."""
    from sqlalchemy import select

    from app.models.intent import Intent, IntentBinding

    rows = (
        await db.execute(
            select(Intent.name, IntentBinding.vertical, IntentBinding.is_builtin)
            .join(Intent, IntentBinding.intent_id == Intent.id)
            .where(IntentBinding.agent_id == agent_id, IntentBinding.user_id.is_(None))
            .order_by(Intent.name, IntentBinding.vertical)
        )
    ).all()
    return [(str(r[0]), r[1], bool(r[2])) for r in rows]


def build_intent_binding_tools(
    user_id: str,
) -> tuple[list[ToolDefinition], dict[str, Callable[..., Awaitable[str]]]]:
    """The intent-binding tools, as definitions plus the handlers to register."""

    def _caller_id() -> uuid.UUID | None:
        return safe_uuid(user_id) if user_id else None

    async def _caller(db: AsyncSession) -> User | None:
        uid = _caller_id()
        return await _builder._load_caller_user_for_builder(db, uid) if uid else None

    async def _agent(db: AsyncSession, user: User, ref: str) -> tuple[Agent | None, str | None]:
        """The agent *ref* names (an id or a slug) among those the caller sees."""
        from sqlalchemy import select

        from app.api.v1.helpers import agent_tool_admin_bypass, get_user_group_ids, user_visible_filter
        from app.models.agent import Agent

        ref = (ref or "").strip()
        if not ref:
            return None, "agent_id is required (an agent id or its slug)."
        group_ids = await get_user_group_ids(db, user.id)
        parsed = safe_uuid(ref)
        clause = Agent.id == parsed if parsed is not None else Agent.slug == ref.lower()
        agent = (
            await db.execute(
                select(Agent).where(
                    clause,
                    user_visible_filter(Agent, user.id, group_ids, is_admin=agent_tool_admin_bypass(user)),
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            return None, f"Agent `{ref[:80]}` not found or you don't have access."
        return agent, None

    async def _intent(db: AsyncSession, ref: str) -> tuple[Intent | None, str | None]:
        """The intent *ref* names (its name, e.g. ``cti.stix_transformer``, or its id)."""
        from sqlalchemy import select

        from app.models.intent import Intent

        ref = (ref or "").strip()
        if not ref:
            return None, "intent is required (an intent name such as `cti.stix_transformer`, or its id)."
        parsed = safe_uuid(ref)
        clause = Intent.id == parsed if parsed is not None else Intent.name == ref
        intent = (await db.execute(select(Intent).where(clause))).scalar_one_or_none()
        if intent is None:
            names = (await db.execute(select(Intent.name).order_by(Intent.name).limit(30))).scalars().all()
            known = ", ".join(f"`{n}`" for n in names) or "none"
            return None, f"Intent `{ref[:120]}` not found. Known intents: {known}."
        return intent, None

    async def _audit(
        db: AsyncSession, user: User, action: str, agent: Agent, summary: str, details: dict[str, object]
    ) -> None:
        from app.services.audit import log_audit

        await log_audit(
            db,
            user=user,
            action=action,
            entity_type="intent_binding",
            entity_id=agent.id,
            entity_name=agent.name,
            summary=summary,
            details={**details, "via": "agent"},
        )

    # -- list ------------------------------------------------------------------

    async def _no_account_agent(db: AsyncSession, ref: str) -> tuple[Agent | None, str | None]:
        """The agent *ref* names for a run with no account (an A2A task, a
        company-managed run): the agents ``get_agent`` shows it, through
        ``caller_visible_filter``."""
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter
        from app.models.agent import Agent

        ref = (ref or "").strip()
        if not ref:
            return None, "agent_id is required (an agent id or its slug)."
        parsed = safe_uuid(ref)
        clause = Agent.id == parsed if parsed is not None else Agent.slug == ref.lower()
        agent = (await db.execute(select(Agent).where(clause, caller_visible_filter(Agent, None)))).scalar_one_or_none()
        if agent is None:
            return None, f"Agent `{ref[:80]}` not found or you don't have access."
        return agent, None

    async def _list_agent_intent_bindings(agent_id: str) -> str:
        from app.core.database import async_session

        async with async_session() as db:
            user = await _caller(db)
            if user is None:
                # A run with no account reads what get_agent shows it: the
                # global bindings of the agents it may see; writes stay refused.
                agent, err = await _no_account_agent(db, agent_id)
                if agent is None:
                    return err or "Agent not found."
                lines = await agent_binding_lines(db, agent, None)
                not_listed = catalog_eligibility(agent)
                winners: list[str] = []
            else:
                agent, err = await _agent(db, user, agent_id)
                if agent is None:
                    return err or "Agent not found."
                lines = await agent_binding_lines(db, agent, user.id)
                not_listed = catalog_eligibility(agent)
                winners = await agent_winner_lines(db, user, agent) if lines else []
        head = f"Intent bindings of **{agent.name}** (`{agent.slug}`):"
        body = [f"  • {line}" for line in lines] or [
            f"  • none: no connected platform offers this agent. Bind it with `{BIND_TOOL}`."
        ]
        tail = []
        if winners:
            tail.append(f"Effective winner per intent and vertical ({WINNER_RULE}):")
            tail.extend(f"  • {line}" for line in winners)
        if not_listed:
            tail.append(f"The intent catalog does not list this agent while {not_listed}, whatever its bindings.")
        return "\n".join([head, *body, *tail])

    # -- bind ------------------------------------------------------------------

    async def _bind_agent_to_intent(
        agent_id: str,
        intent: str,
        vertical: str = "",
        priority: int = 0,
        is_locked: bool = False,
    ) -> str:
        from app.core.database import async_session
        from app.services.intent_binding_writes import IntentBindingRefusedError, upsert_intent_binding

        try:
            priority_value = int(priority)
        except (TypeError, ValueError):
            return f"priority must be an integer; got {str(priority)[:40]}."
        vertical_value = (vertical or "").strip() or None
        if vertical_value and vertical_value.lower() in {"all", "*", "any"}:
            vertical_value = None
        async with async_session() as db:
            user = await _caller(db)
            if user is None:
                return _ANONYMOUS_REFUSAL
            agent, err = await _agent(db, user, agent_id)
            if agent is None:
                return err or "Agent not found."
            target, err = await _intent(db, intent)
            if target is None:
                return err or "Intent not found."
            try:
                binding = await upsert_intent_binding(
                    db,
                    user,
                    target,
                    agent,
                    vertical=vertical_value,
                    priority=priority_value,
                    is_locked=bool(is_locked),
                    refuse_builtin=True,
                )
                await db.flush()
                await _audit(
                    db,
                    user,
                    "bind",
                    agent,
                    f"Bound agent '{agent.name}' to intent '{target.name}'",
                    {"intent": target.name, "vertical": vertical_value, "binding_id": str(binding.id)},
                )
                await db.commit()
                await db.refresh(binding)
            except IntentBindingRefusedError as exc:
                await db.rollback()
                return f"Nothing was bound: {exc.detail}"
            except Exception:
                logger.exception("Agent tool bind of agent %s to intent %s failed", agent.id, target.id)
                await db.rollback()
                return "Failed to bind the agent to the intent. Please try again."
            line = binding_line(binding, target.name, user.id)
            not_listed = catalog_eligibility(agent)
            try:
                winners = await agent_winner_lines(db, user, agent, intent_id=target.id)
            except Exception:
                # The binding is committed: a failed read only loses the winner lines.
                logger.exception("Winner read of intent %s failed after the bind", target.id)
                winners = [f"winner not read: check it with `{LIST_TOOL}`"]
        out = [f"Agent **{agent.name}** bound: {line}."]
        out.append(f"Effective winner ({WINNER_RULE}):")
        out.extend(f"  • {w}" for w in winners)
        if binding.user_id is not None:
            out.append(
                "This is your personal override: only you see it. A global binding, which every user "
                "of a connected platform sees, is an administrator's."
            )
        if not_listed:
            out.append(f"The intent catalog does not list this agent while {not_listed}.")
        else:
            out.append(
                f"The catalog lists it now for `{target.name}`"
                + (f" in vertical `{vertical_value}`." if vertical_value else " in every vertical.")
            )
        return "\n".join(out)

    # -- unbind ----------------------------------------------------------------

    async def _unbind_agent_from_intent(agent_id: str, intent: str, vertical: str = "", binding_id: str = "") -> str:
        from sqlalchemy import select

        from app.core.database import async_session
        from app.models.intent import IntentBinding
        from app.services.intent_binding_writes import IntentBindingRefusedError, check_intent_binding_delete

        async with async_session() as db:
            user = await _caller(db)
            if user is None:
                return _ANONYMOUS_REFUSAL
            agent, err = await _agent(db, user, agent_id)
            if agent is None:
                return err or "Agent not found."
            target, err = await _intent(db, intent)
            if target is None:
                return err or "Intent not found."
            query = select(IntentBinding).where(
                IntentBinding.agent_id == agent.id, IntentBinding.intent_id == target.id
            )
            if (binding_id or "").strip():
                parsed = safe_uuid(binding_id.strip())
                if parsed is None:
                    return f"binding_id must be a binding id as {LIST_TOOL} shows it."
                query = query.where(IntentBinding.id == parsed)
            else:
                vertical_value = (vertical or "").strip() or None
                if vertical_value and vertical_value.lower() in {"all", "*", "any"}:
                    vertical_value = None
                query = query.where(
                    IntentBinding.vertical.is_(None)
                    if vertical_value is None
                    else IntentBinding.vertical == vertical_value
                )
                owner = IntentBinding.user_id.is_(None) if user.is_admin else IntentBinding.user_id == user.id
                query = query.where(owner)
            rows = list((await db.execute(query)).scalars().all())
            if not rows:
                return f"No binding of **{agent.name}** to `{target.name}` matches. Read them with `{LIST_TOOL}`."
            if len(rows) > 1:
                return f"Several bindings match; name one with binding_id (see `{LIST_TOOL}`)."
            binding = rows[0]
            try:
                check_intent_binding_delete(user, binding, refuse_builtin=True)
                line = binding_line(binding, target.name, user.id)
                await db.delete(binding)
                await _audit(
                    db,
                    user,
                    "unbind",
                    agent,
                    f"Unbound agent '{agent.name}' from intent '{target.name}'",
                    {"intent": target.name, "vertical": binding.vertical, "binding_id": str(binding.id)},
                )
                await db.commit()
            except IntentBindingRefusedError as exc:
                await db.rollback()
                return f"Nothing was removed: {exc.detail}"
            except Exception:
                logger.exception("Agent tool unbind of binding %s failed", binding.id)
                await db.rollback()
                return "Failed to remove the binding. Please try again."
        return f"Removed: {line}."

    handlers: dict[str, Callable[..., Awaitable[str]]] = {
        LIST_TOOL: _list_agent_intent_bindings,
        BIND_TOOL: _bind_agent_to_intent,
        UNBIND_TOOL: _unbind_agent_from_intent,
    }
    return _definitions(), handlers


def _definitions() -> list[ToolDefinition]:
    agent_ref = {"type": "string", "description": "Agent id or slug."}
    intent_ref = {"type": "string", "description": "Intent name (cti.stix_transformer) or id."}
    vertical = {"type": "string", "description": "Vertical (cti, aev); empty: all."}
    return [
        ToolDefinition(
            name=LIST_TOOL,
            description=(
                "An agent's intent bindings (what makes a connected platform offer it), the winner per "
                "intent and vertical (higher priority wins) and whether the catalog lists it."
            ),
            input_schema={"type": "object", "properties": {"agent_id": agent_ref}, "required": ["agent_id"]},
            source="builtin:platform",
        ),
        ToolDefinition(
            name=BIND_TOOL,
            description=(
                "Bind an agent to an intent: admins write the default (agent shared or company-managed), "
                "others a personal override. Higher priority wins (tie: first bound); names the winner "
                "and the priority needed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": agent_ref,
                    "intent": intent_ref,
                    "vertical": vertical,
                    "priority": {"type": "integer", "description": "Higher wins."},
                    "is_locked": {"type": "boolean", "description": "Admins: no personal override."},
                },
                "required": ["agent_id", "intent"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name=UNBIND_TOOL,
            description="Remove an agent's intent binding (built-in ones are kept).",
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": agent_ref,
                    "intent": intent_ref,
                    "vertical": vertical,
                    "binding_id": {"type": "string"},
                },
                "required": ["agent_id", "intent"],
            },
            source="builtin:platform",
        ),
    ]
