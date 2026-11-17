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

"""Variable resolution service – replaces ``{{var_id.UUID}}`` placeholders at runtime.

Used by the agentic loop to substitute variables in:
- Agent persona and guardrails
- Assignment prompts
- Skill content (when injected into system prompt)
- Prompt content (when injected into system prompt)
- Custom tool JS functions

Each reference points to a specific variable by UUID, eliminating key-based
shadowing where a user-owned variable could override a company-managed one.
Visibility is still enforced: only variables the current user can see are resolved.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid as _uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redaction import defuse_secret_markers, remember_secret_value, restore_known_secrets
from app.core.utils import safe_uuid
from app.models.variable import Variable
from app.services.vault import SecretUnavailableError, reveal_field

logger = logging.getLogger(__name__)

#: How many secret variables one load reads at once (a vault read each when not cached).
_MAX_CONCURRENT_SECRET_READS = 8

VAR_ID_PATTERN = re.compile(
    r"\{\{var_id\.([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\}\}",
    re.IGNORECASE,
)


async def load_variables(
    db: AsyncSession,
    user_id: _uuid.UUID | str | None,
    user_group_ids: list[str] | None = None,
) -> dict[str, str]:
    """Load the variables *user_id* may see as a ``{uuid_str: resolved_value}`` dict.

    Visibility is the user's own three tiers (``user_visible_filter``: own,
    shared with the user or one of *user_group_ids*, company-managed unless
    narrowed by ``access_restricted``), never an admin bypass: a value is
    substituted into what the model reads, so a secret resolves only for a
    run whose identity may read it. A caller with no account (``None`` or a
    sentinel such as ``"anonymous"``) resolves the company-managed variables.

    Secret values are decrypted transparently, and remembered in the open
    known-secrets scope (``core/redaction.py``): what the run stores, traces
    and logs holds a marker instead, restored where its context is rebuilt.
    """
    from app.api.v1.helpers import user_visible_filter

    user_uuid = user_id if isinstance(user_id, _uuid.UUID) else safe_uuid(user_id)
    visible = (
        Variable.is_company_managed.is_(True)
        if user_uuid is None
        else user_visible_filter(Variable, user_uuid, user_group_ids)
    )
    result = await db.execute(
        select(Variable).where(visible).order_by(Variable.created_at)
    )
    all_vars: Sequence[Variable] = result.scalars().all()
    # Many secrets in vaults must not become as many simultaneous vault reads;
    # the resolver still shares one read between references to the same secret.
    limit = asyncio.Semaphore(_MAX_CONCURRENT_SECRET_READS)

    async def _secret_value(var: Variable) -> str | None:
        try:
            async with limit:
                return await reveal_field(var, "value_enc") or ""
        except SecretUnavailableError:
            # A vault reference that cannot be read: the placeholder
            # stays in the text, the reason is on the variable and in the log.
            return None
        except Exception:
            logger.warning("Failed to decrypt variable %s (id=%s)", var.name, var.id)
            return ""

    secrets = [var for var in all_vars if var.is_secret and var.value_enc]
    secret_values = dict(
        zip((var.id for var in secrets), await asyncio.gather(*(_secret_value(var) for var in secrets)), strict=True)
    )
    # Substituted into what the model reads, so kept out of what the run stores,
    # traces and logs, and restored where its context is rebuilt (core/redaction.py).
    for variable_id, value in secret_values.items():
        remember_secret_value(variable_id, value)

    resolved: dict[str, str] = {}
    for var in all_vars:
        val = secret_values.get(var.id, var.value or "")
        if val is not None:
            resolved[str(var.id)] = val
    return resolved


def restore_secrets_in_history(history: list) -> list:
    """*history* (``ChatMessage`` rows read back from storage) with the open scope's secret markers restored.

    What a run or turn stores holds each secret variable value as its marker
    (``core/redaction.py``); the model is given back the values, after
    :func:`load_variables` resolved them: content, reasoning, provider replay
    data and tool-call arguments. A user message is what a person wrote: its
    markers are defused, never restored. A message holding no marker is the
    same object; a changed one is a copy, so the stored rows are never touched.
    """
    from dataclasses import replace

    from app.tools.base import ToolCall

    restored: list = []
    for message in history:
        changes: dict = {}
        read_back = defuse_secret_markers if getattr(message, "role", None) == "user" else restore_known_secrets
        for field_name in ("content", "thinking", "_provider_extra"):
            value = getattr(message, field_name, None)
            new = read_back(value)
            if new is not value:
                changes[field_name] = new
        calls = getattr(message, "tool_calls", None)
        if calls:
            new_calls = [
                call if (args := restore_known_secrets(call.arguments)) is call.arguments
                else ToolCall(id=call.id, name=call.name, arguments=args)
                for call in calls
            ]
            if any(new is not old for new, old in zip(new_calls, calls, strict=True)):
                changes["tool_calls"] = new_calls
        restored.append(replace(message, **changes) if changes else message)
    return restored


def substitute_variables(text: str | None, variables: dict[str, str]) -> str:
    """Replace ``{{var_id.UUID}}`` placeholders in *text* with resolved values.

    Unresolved placeholders are left as-is so users can spot missing variables.
    """
    if not text:
        return text or ""
    if not variables:
        return text

    def _replacer(m: re.Match) -> str:
        var_id = m.group(1).lower()
        return variables.get(var_id, m.group(0))

    return VAR_ID_PATTERN.sub(_replacer, text)


def extract_variable_ids(text: str | None) -> list[str]:
    """Extract all ``{{var_id.UUID}}`` IDs from a text string."""
    if not text:
        return []
    return list({m.lower() for m in VAR_ID_PATTERN.findall(text)})


def rewrite_variable_ids(text: str | None, id_map: dict[str, str]) -> str:
    """Rewrite ``{{var_id.OLD_UUID}}`` references using *id_map* ``{old: new}``.

    Used during import/clone to remap variable UUIDs to newly created records.
    """
    if not text or not id_map:
        return text or ""

    def _replacer(m: re.Match) -> str:
        old_id = m.group(1).lower()
        new_id = id_map.get(old_id)
        if new_id:
            return "{{var_id." + new_id + "}}"
        return m.group(0)

    return VAR_ID_PATTERN.sub(_replacer, text)


async def missing_variable_ids(db: AsyncSession, variable_ids: Sequence[str]) -> set[str]:
    """The ids among *variable_ids* that no variable carries - deleted, or
    named by an agent copied from another install. Existence only, for the
    diagnosis of a placeholder a run left unresolved: whatever else it names
    exists and is invisible to the identity the run resolved as."""
    wanted: dict[_uuid.UUID, str] = {}
    missing: set[str] = set()
    for vid in variable_ids:
        try:
            parsed = _uuid.UUID(str(vid))
        except ValueError:
            missing.add(vid)
            continue
        wanted[parsed] = str(parsed)
    if wanted:
        result = await db.execute(select(Variable.id).where(Variable.id.in_(wanted)))
        found = set(result.scalars().all())
        missing.update(text for vid, text in wanted.items() if vid not in found)
    return missing


def variable_has_value(variable: Variable) -> bool:
    """Whether a variable resolves to something: a secret keeps its value
    encrypted in ``value_enc``, any other in ``value``."""
    return bool(variable.value_enc if variable.is_secret else variable.value)


async def agent_referenced_variable_ids(db: AsyncSession, agent) -> list[str]:
    """Every ``{{var_id.<uuid>}}`` the agent's run resolves: its persona and
    guardrails, its assignments' prompts, and the content of its prompts,
    skills and custom tools."""
    from app.models.agent_skill import AgentSkill
    from app.models.assignment import Assignment
    from app.models.custom_tool import CustomTool
    from app.models.prompt import Prompt

    def _uuids(ids) -> list[_uuid.UUID]:
        out = []
        for raw in ids or []:
            try:
                out.append(_uuid.UUID(str(raw)))
            except ValueError:
                continue
        return out

    texts: list[str | None] = [agent.persona, agent.guardrails]
    rows = await db.execute(
        select(Assignment.prompt, Assignment.activation_prompt).where(
            Assignment.agent_id == agent.id
        )
    )
    for prompt, activation_prompt in rows.all():
        texts.extend([prompt, activation_prompt])
    for columns, ids in (
        ((Prompt.content,), agent.prompt_ids),
        ((AgentSkill.content,), agent.skill_ids),
        ((CustomTool.js_function, CustomTool.description), agent.custom_tool_ids),
    ):
        if uuids := _uuids(ids):
            model = columns[0].class_
            result = await db.execute(select(*columns).where(model.id.in_(uuids)))
            for row in result.all():
                texts.extend(row)
    return sorted({vid for text in texts for vid in extract_variable_ids(text)})


async def agent_variables_needing_value(
    db: AsyncSession, agent, user_id: _uuid.UUID, user_group_ids: list[str]
) -> list[Variable]:
    """The variables the agent references that ``user`` can see and that have
    no value yet - a secret copied without its value by a catalog install, or
    a variable nobody filled in. Visibility is the user's own (never an admin
    bypass): a run does not resolve a variable its user cannot see either."""
    from app.api.v1.helpers import user_visible_filter

    ids = [_uuid.UUID(vid) for vid in await agent_referenced_variable_ids(db, agent)]
    if not ids:
        return []
    result = await db.execute(
        select(Variable)
        .where(Variable.id.in_(ids), user_visible_filter(Variable, user_id, user_group_ids))
        .order_by(Variable.name)
    )
    return [v for v in result.scalars().all() if not variable_has_value(v)]


async def agent_deleted_variable_ids(db: AsyncSession, agent) -> list[str]:
    """The variables the agent references that no variable carries any more -
    deleted, or named by an agent copied from another install. Their
    placeholder reaches the model as written, and there is no name to show."""
    return sorted(await missing_variable_ids(db, await agent_referenced_variable_ids(db, agent)))


async def load_owner_visible_variable_records(
    db: AsyncSession,
    variable_ids: list[str],
    owner_id: _uuid.UUID | str | None,
) -> list[Variable]:
    """The referenced variables an agent's OWNER can see, for disclosure.

    What an export, a catalog page or a catalog install may say about a
    ``{{var_id.<uuid>}}`` in an agent's text is bounded by the owner's own
    three-tier visibility (``user_visible_filter``, never an admin bypass - a
    run does not resolve every variable for an admin either). Anyone can
    publish their own agent, so an id typed into a persona that names someone
    else's variable must not turn the catalog into a way to read it. An agent
    with no owner discloses unrestricted company-managed variables only.
    """
    from app.api.v1.helpers import (
        company_wide_visible_filter,
        get_user_group_ids,
        user_visible_filter,
    )

    uuids = []
    for vid in variable_ids or []:
        try:
            uuids.append(_uuid.UUID(str(vid)))
        except (ValueError, AttributeError):
            continue
    if not uuids:
        return []
    owner_uuid: _uuid.UUID | None
    try:
        owner_uuid = _uuid.UUID(str(owner_id)) if owner_id else None
    except ValueError:
        owner_uuid = None
    if owner_uuid is None:
        visible = company_wide_visible_filter(Variable)
    else:
        group_ids = await get_user_group_ids(db, owner_uuid)
        visible = user_visible_filter(Variable, owner_uuid, group_ids)
    result = await db.execute(select(Variable).where(Variable.id.in_(uuids), visible))
    return list(result.scalars().all())
