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

"""Shared agent-import pipeline (helper module, not a router).

Every way of importing an agent funnels through :func:`create_agent_from_bundle`:

- ``POST /agents/import`` — a native export bundle as a JSON body;
- ``POST /agents/import-file`` — an uploaded file in either supported format,
  auto-detected and mapped onto the native bundle shape by
  :mod:`app.services.agentschema`.

Keeping one implementation is the point. Before this module existed the import
path had already drifted away from ``create_agent``: imported agents got no
initial persona/config version history and never received the platform
default built-in tool set. A second, AgentSchema-specific creation path
would have inherited both gaps and added a third place to fix them, so the
AgentSchema format is a *mapping* onto this pipeline rather than a pipeline of
its own.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import (
    generate_unique_agent_slug,
    get_user_group_ids,
    lower_budget_to_ceiling,
    require_allowed_mcp_server_url,
    user_visible_filter,
)
from app.core.lone_surrogates import find_lone_surrogate, lone_surrogate_error
from app.core.utils import ensure_webhook_secret, safe_uuid, validate_schedule_trigger_config
from app.llm.agent_model import LLM_MODEL_SOURCE_ROUTING, normalize_llm_model_source
from app.models.agent import Agent
from app.models.agent_copy import (
    AGENT_BUNDLE_PORTABLE_FIELDS,
    REDACTED_MCP_SERVER_ID,
    build_agent_import_kwargs,
    import_integration_override,
)
from app.models.agent_skill import AgentSkill
from app.models.assignment import (
    ASSIGNMENT_BOUNDED_TEXT_FIELDS,
    Assignment,
    assignment_text_length_error,
)
from app.models.custom_tool import CustomTool
from app.models.general_assistant_config import clamp_max_iterations
from app.models.integration import Integration
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt
from app.models.user import User
from app.models.variable import Variable
from app.services.agent_config_versioning import seed_initial_agent_versions
from app.services.assignment_activation_check import stored_activation_check
from app.services.assignment_pause import (
    actor_display_name,
    pause_assignment,
)
from app.services.audit import log_audit
from app.services.builtin_tools.filtering import fold_unbound_instance_scopes
from app.services.company_managed_writes import portable_coordination_access
from app.services.element_scope import normalize_scope_enforcement
from app.services.variable_resolver import rewrite_variable_ids

logger = logging.getLogger(__name__)

#: Built-in tools a newly created agent gets when it does not ask for a
#: specific set. Single source of truth for ``create_agent`` and every import
#: path, so an imported agent never silently lacks ``inspect_my_traces``.
DEFAULT_BUILTIN_TOOL_IDS: tuple[str, ...] = ("inspect_my_traces",)

#: Native bundle versions this pipeline can read.
SUPPORTED_BUNDLE_VERSIONS = (1, 2, 3)


@dataclass(slots=True)
class ImportedAgent:
    """Outcome of an import, for the endpoint to shape into a response."""

    agent: Agent
    assignment_count: int
    #: Non-fatal observations worth showing the user (placeholder tools that
    #: need finishing, dropped model overrides, references this instance
    #: cannot resolve).
    warnings: list[str] = field(default_factory=list)


def _require_importable(bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate the bundle envelope, returning the agent section.

    The file-upload path has already run a fuller verification loop, but the
    JSON-body path arrives here straight from the request, and a bundle
    assembled by an API client can be anything.
    """
    version = bundle.get("version")
    if version not in SUPPORTED_BUNDLE_VERSIONS:
        raise HTTPException(
            status_code=400, detail=f"Unsupported export version: {version}"
        )
    export_type = bundle.get("export_type", "agent")
    if export_type != "agent":
        raise HTTPException(
            status_code=400, detail=f"Unsupported export type: {export_type}"
        )
    agent_data = bundle.get("agent")
    if not isinstance(agent_data, dict) or not agent_data.get("name"):
        raise HTTPException(
            status_code=400, detail="Agent name is required in the export bundle"
        )
    _refuse_unwritable_text(bundle, agent_data)
    return agent_data


#: The text a bundle entry hands to a column as sent, section by section in the
#: order the import writes them: section, label, model, bundle keys (each a
#: column of the model). ``name`` is read the way the import reads it.
_BUNDLE_TEXT_FIELDS: tuple[tuple[str, str, Any, tuple[str, ...]], ...] = (
    ("variables", "variable", Variable, ("name", "value", "description")),
    ("custom_tools", "custom tool", CustomTool, ("name", "description", "icon_url", "js_function")),
    ("mcp_servers", "MCP server", MCPServer, ("name", "transport", "command", "url")),
    ("skills", "skill", AgentSkill, ("name", "description", "url", "content", "compatibility", "license")),
    ("prompts", "prompt", Prompt, ("name", "description", "content")),
)
#: The model override is left out: a model or provider that its column cannot
#: hold is dropped, like an unlisted model.
_AGENT_TEXT_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "icon",
    "persona",
    "output_format",
    "handover_prompt",
    "reasoning_effort",
    "guardrails",
    "human_in_the_loop_prompt",
    # String(10), carried as sent by the bundle (#11533, #11598).
    "organization_context_mode",
)
#: An assignment's nullable text; its ``String(n)`` lengths are
#: ``_refuse_over_long_assignment_text``'s.
_ASSIGNMENT_TEXT_FIELDS: tuple[str, ...] = (
    "description",
    "activation_prompt",
    "delay",
    "ai_label_prompt",
    "human_in_the_loop_prompt",
)
#: The other text columns of those models, and why this check leaves them alone
#: (``test_every_text_column_an_import_writes_is_checked``).
_UNCHECKED_TEXT_COLUMNS: dict[Any, dict[str, str]] = {
    Variable: {"value_enc": "A secret's value is not imported."},
    CustomTool: {
        "language": "Written from a closed set, never as sent.",
        "test_code": "Declared tests are not imported with an agent bundle.",
        "last_test_status": "Written by the server.",
        **dict.fromkeys(
            ("source_path", "source_commit_sha", "source_content_hash"),
            "A repository source's link, set by its sync, never imported.",
        ),
    },
    MCPServer: {
        **dict.fromkeys(
            (
                "env_enc",
                "headers_enc",
                "auth_mode",
                "client_id_enc",
                "client_secret_enc",
                "access_token_enc",
                "refresh_token_enc",
                "scopes",
                "authorize_url",
                "token_url",
                "registration_url",
                "oauth_resource",
                "oauth_metadata_url",
                "oauth_status",
                "credential_mode",
            ),
            "Credentials and OAuth state are not imported.",
        ),
        **dict.fromkeys(
            ("investigation_tool_name", "investigation_argument"),
            "The investigation designation is set on the server, never imported.",
        ),
    },
    AgentSkill: {"source_ref": "Not imported."},
    Assignment: {
        "prompt": "A Text column the assignment loop's entry reader type-checks.",
        "paused_reason": "Written by the server.",
        "paused_by_label": "Written by the server.",
        "paused_details": "Written by the server.",
    },
}


def _refuse_unwritable_text(bundle: dict[str, Any], agent_data: dict[str, Any]) -> None:
    """Refuse a text value its column cannot take, before anything is written.

    A string longer than its ``String(n)`` column, or anything but a string in
    a text column, reached the INSERT and answered a 500 after the first rows
    of the import were flushed. The 400 names the entity, the field and the
    limit (or the type). Only what the import writes is checked, in the order
    it reads it - the assignments' lengths first, then each section with
    its entry reader, the agent, and the assignments' other text - so the first
    refusal is the first problem the import itself would stop at.
    """
    from app.core.utils import text_column_error

    _refuse_lone_surrogates_in_assignments(bundle.get("assignments"))
    _refuse_over_long_assignment_text(bundle.get("assignments"))

    for section, label, model, keys in _BUNDLE_TEXT_FIELDS:
        entries = bundle.get(section)
        if not isinstance(entries, list):
            continue
        columns = model.__table__.c
        for position, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue
            if model is Variable:
                if not entry.get("id"):
                    continue  # skipped by the import
                name_key, name = _variable_name(entry)
            else:
                name_key, name = "name", _entry_text(entry, "name", "", section=label)
            where = f"'{name}'" if name.strip() else f"#{position}"
            for key in keys:
                if key == "value" and entry.get("is_secret"):
                    continue  # a secret value is not imported
                error = (
                    text_column_error(columns[key], name, field=name_key)
                    if key == "name"
                    else text_column_error(columns[key], entry.get(key))
                )
                if error is not None:
                    at = f"#{position}" if key == "name" else where
                    raise HTTPException(
                        status_code=400, detail=f"Imported {label} {at}: {error}"
                    )

    columns = Agent.__table__.c
    for key in _AGENT_TEXT_FIELDS:
        error = text_column_error(columns[key], agent_data.get(key))
        if error is not None:
            raise HTTPException(status_code=400, detail=f"Imported agent: {error}")

    entries = bundle.get("assignments")
    columns = Assignment.__table__.c
    for position, entry in enumerate(entries if isinstance(entries, list) else [], start=1):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        where = f"'{name}'" if isinstance(name, str) and name.strip() else f"#{position}"
        for key in _ASSIGNMENT_TEXT_FIELDS:
            error = text_column_error(columns[key], entry.get(key))
            if error is not None:
                raise HTTPException(
                    status_code=400, detail=f"Imported assignment {where}: {error}"
                )


def _variable_name(entry: dict[str, Any]) -> tuple[str, str]:
    """The bundle key an imported variable's name is read from, and the name.

    ``Variable`` has no ``key`` column - the field is ``name``. A legacy bundle
    that still carries ``key`` is read through it when ``name`` is empty; the
    name is ``""`` when neither is given.
    """
    name = _entry_text(entry, "name", "", section="variable")
    if name:
        return "name", name
    return "key", _entry_text(entry, "key", "", section="variable")


def _entry_text(entry: dict[str, Any], key: str, default: str, *, section: str) -> str:
    """Read a text field an import writes into a NOT NULL column.

    A bundle is untyped JSON: ``entry.get(key, default)`` hands an explicit
    ``null`` to the INSERT as ``None`` (an ``IntegrityError``) and a number as a
    ``DataError``, both a 500. ``null`` reads as an absent key and gets the same
    default; any other non-string is refused with a 400 naming the field.
    """
    value = entry.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Imported {section} entry '{key}' must be a string, got "
                f"{type(value).__name__}."
            ),
        )
    return value


#: Written as an imported or copied assignment's whole MCP scope when none of
#: the servers it named can be used here: the placeholder a public download
#: writes, the nil UUID, which is never the id of a server. The assignment stays
#: scoped to no server - an empty ``tool_sources`` means every server of the
#: agent to the assignment engine.
UNRESOLVED_TOOL_SOURCE = REDACTED_MCP_SERVER_ID


def _bundle_tool_sources(wf_data: dict[str, Any], *, name: str) -> list[str] | None:
    """An imported assignment's ``tool_sources``, refused unless well-formed.

    Same contract as the assignment API: a list of strings, or null. Coercing
    anything else to ``[]`` would widen the scope to every server of the agent.
    An absent key and ``null`` both mean every server of the agent (the column
    is nullable), and ``null`` is kept as ``null`` so an exported null scope
    round-trips unchanged.
    """
    value = wf_data.get("tool_sources", [])
    if value is None:
        return None
    if isinstance(value, list):
        bad = [entry for entry in value if not isinstance(entry, str)]
        if not bad:
            return value
        got = f"a list with an entry of type {type(bad[0]).__name__}"
    else:
        got = type(value).__name__
    raise HTTPException(
        status_code=400,
        detail=(
            f"Imported assignment '{name}' has an invalid "
            f"tool_sources: must be a list of MCP server references, got {got}."
        ),
    )


def resolve_bundle_tool_sources(
    value: list[str] | None,
    *,
    id_by_ref: dict[str, str],
    visible_ids: set[str],
) -> tuple[list[str] | None, int]:
    """Map an imported assignment's ``tool_sources`` onto servers usable here.

    Returns the scope to store and the number of entries dropped. An entry is
    kept when it is the ``ref`` of a server this import recreated (mapped onto
    the new id) or the id of a server the importer can see (a
    same-instance round trip). Anything else - a source-instance id, a ref no
    server of the file carries, a redacted placeholder - is dropped: the
    assignment engine loads every id of the list with no visibility check, so
    an id carried by a file must never reach it unchecked. A non-empty scope
    with nothing left becomes :data:`UNRESOLVED_TOOL_SOURCE`, never ``[]``.
    """
    if value is None:
        return None, 0
    kept: list[str] = []
    dropped = 0
    for entry in value:
        server_id = id_by_ref.get(entry)
        if server_id is None:
            parsed = safe_uuid(entry)
            if parsed is not None and str(parsed) in visible_ids:
                server_id = str(parsed)
        if server_id is None:
            if entry != UNRESOLVED_TOOL_SOURCE:
                dropped += 1
            continue
        if server_id not in kept:
            kept.append(server_id)
    if value and not kept:
        return [UNRESOLVED_TOOL_SOURCE], dropped
    return kept, dropped


def _referenced_server_ids(
    assignments: Any, id_by_ref: dict[str, str]
) -> set[uuid.UUID]:
    """Every id-shaped ``tool_sources`` entry that is not a ref of the file."""
    found: set[uuid.UUID] = set()
    for wf_data in assignments if isinstance(assignments, list) else []:
        sources = wf_data.get("tool_sources") if isinstance(wf_data, dict) else None
        for entry in sources if isinstance(sources, list) else []:
            if isinstance(entry, str) and entry not in id_by_ref:
                parsed = safe_uuid(entry)
                if parsed is not None:
                    found.add(parsed)
    return found


async def _visible_mcp_server_ids(
    db: AsyncSession, user: User, candidates: set[uuid.UUID]
) -> set[str]:
    """The ids among ``candidates`` of MCP servers ``user`` can see.

    The visibility an assignment write checks a server it adds against
    (three-tier, admins bypass, ``binding_access.refused_additions``), so an
    import keeps exactly the servers its importer could bind by hand.
    """
    if not candidates:
        return set()
    group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(MCPServer.id).where(
            MCPServer.id.in_(candidates),
            user_visible_filter(MCPServer, user.id, group_ids, is_admin=user.is_admin),
        )
    )
    return {str(server_id) for server_id in result.scalars().all()}


def _referenced_trigger_integration_ids(assignments: Any) -> set[uuid.UUID]:
    """Every id-shaped integration the file's assignment triggers name."""
    from app.api.v1.binding_access import trigger_integration_ref

    found: set[uuid.UUID] = set()
    for wf_data in assignments if isinstance(assignments, list) else []:
        config = wf_data.get("trigger_config") if isinstance(wf_data, dict) else None
        ref = trigger_integration_ref(config)
        parsed = safe_uuid(ref) if isinstance(ref, str) else None
        if parsed is not None:
            found.add(parsed)
    return found


async def _visible_integration_ids(
    db: AsyncSession, user: User, candidates: set[uuid.UUID]
) -> set[str]:
    """The ids among ``candidates`` of integrations ``user`` can see.

    The rule a trigger write checks its integration against (three-tier,
    admins bypass, ``binding_access.trigger_integration_refused``) and the one
    the import applies to MCP servers (:func:`_visible_mcp_server_ids`), so an
    import keeps exactly the trigger integrations its importer could name by
    hand; an id naming no integration is dropped for everyone.
    """
    if not candidates:
        return set()
    group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(Integration.id).where(
            Integration.id.in_(candidates),
            user_visible_filter(Integration, user.id, group_ids, is_admin=user.is_admin),
        )
    )
    return {str(integration_id) for integration_id in result.scalars().all()}


def _importable_trigger_config(
    trigger_config: dict[str, Any], visible_ids: set[str]
) -> tuple[dict[str, Any], bool]:
    """``trigger_config`` without the integration it names unless the importer
    can see it, and whether it was removed. An integration the file
    names that is not a UUID, or that names nothing here, is removed too."""
    from app.api.v1.binding_access import (
        trigger_integration_ref,
        without_trigger_integration,
    )

    ref = trigger_integration_ref(trigger_config)
    if ref is None:
        return trigger_config, False
    parsed = safe_uuid(ref) if isinstance(ref, str) else None
    if parsed is not None and str(parsed) in visible_ids:
        return trigger_config, False
    return without_trigger_integration(trigger_config), True


def _dropped_trigger_integration_warning(name: str) -> str:
    return (
        f"Assignment '{name}' named a trigger integration that you cannot use "
        "here; it was removed from the trigger. Pick one of your integrations "
        "before you enable it."
    )


def _dropped_tool_sources_warning(
    name: str, dropped: int, tool_sources: list[str] | None
) -> str:
    if tool_sources == [UNRESOLVED_TOOL_SOURCE]:
        return (
            f"Assignment '{name}' was limited to MCP servers that are not in "
            "this file and that you cannot use here; it runs without MCP tools "
            "rather than with every server of the agent."
        )
    return (
        f"Assignment '{name}' was limited to {dropped} MCP server(s) that are "
        "not in this file and that you cannot use here; they were removed from "
        "its scope."
    )


def _resolve_builtin_tool_ids(agent_data: dict[str, Any]) -> list[str]:
    """Built-in tool ids for the imported agent.

    A bundle that *states* its built-in tools is honoured exactly, including an
    explicit empty list ("this agent has none on purpose"). A bundle that says
    nothing gets the platform default, matching ``create_agent``: a v1/v2 or
    hand-written bundle would otherwise import with no built-in tools at all
    and silently lose self-trace inspection.
    """
    declared = agent_data.get("builtin_tool_ids")
    if isinstance(declared, list):
        return [str(item) for item in declared]
    return list(DEFAULT_BUILTIN_TOOL_IDS)


async def _seed_initial_versions(
    db: AsyncSession, *, agent: Agent, author_id: uuid.UUID
) -> None:
    """Give the imported agent the version history a created agent gets.

    ``create_agent`` and ``duplicate_agent`` both seed an "Initial version" for
    the persona and for every tracked config field; the import path did not,
    leaving imported agents with an empty Versioning tab and no
    baseline to roll back to. Same call sequence as ``create_agent`` so the two
    cannot drift again.
    """
    await seed_initial_agent_versions(db, agent=agent, author_id=author_id)


def _refuse_lone_surrogates_in_assignments(entries: object) -> None:
    """Refuse an assignment entry holding a lone UTF-16 surrogate anywhere.

    In a JSON column (``trigger_config``) it is stored as an escape no later
    read can parse, in a text column refused at the write. Checked first, so
    no other refusal echoes it: the entry is named by its position when its
    name holds one.
    """
    if not isinstance(entries, list):
        return
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        error = lone_surrogate_error("", entry)
        if error is None:
            continue
        name = entry.get("name")
        label = (
            f"'{name}'"
            if isinstance(name, str) and name.strip() and find_lone_surrogate(name) is None
            else f"#{position}"
        )
        raise HTTPException(status_code=422, detail=f"Imported assignment {label}: {error}")


def _refuse_over_long_assignment_text(entries: object) -> None:
    """Refuse an assignment value longer than its ``String(n)`` column.

    Checked before anything is created, not in the assignment loop, so a
    refused bundle writes no variable, tool or agent either. An entry that is
    not an object is left to the loop's own refusal, and a field that is not a
    string to its type check.
    """
    if not isinstance(entries, list):
        return
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        for key in ASSIGNMENT_BOUNDED_TEXT_FIELDS:
            error = assignment_text_length_error(key, entry.get(key))
            if error is None:
                continue
            name = entry.get("name")
            label = (
                f"'{name}'"
                if key != "name" and isinstance(name, str) and name.strip()
                else f"#{position}"
            )
            raise HTTPException(
                status_code=400, detail=f"Imported assignment {label}: {error}"
            )


async def _import_bundle_memories(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    entries: object,
    warnings: list[str],
) -> int:
    """Recreate a native bundle's optional ``memories`` on the new agent.

    The importer owns the new agent, so every scope is theirs to write. An
    entry scoped to a user this platform does not have, or one the store
    refuses (empty, tradecraft), is left out with a warning rather than
    failing the import. Returns the number of entries recreated.
    """
    from pydantic import ValidationError

    from app.api.v1.agent_memory import ImportMemoryEntry, unknown_user_ids
    from app.services.persistent_memory import (
        MemoryImportEntry,
        MemoryImportError,
        import_memories,
        prepare_import_entry,
    )

    if not isinstance(entries, list) or not entries:
        return 0
    parsed: list[MemoryImportEntry] = []
    malformed = 0
    for index, raw in enumerate(entries):
        try:
            entry = MemoryImportEntry(**ImportMemoryEntry.model_validate(raw).model_dump())
            prepare_import_entry(index, entry)
        except (ValidationError, MemoryImportError):
            malformed += 1
            continue
        parsed.append(entry)
    unknown = await unknown_user_ids(
        db, {e.user_id for e in parsed if e.user_id is not None}
    )
    importable = [e for e in parsed if e.user_id not in unknown]
    if len(importable) < len(parsed):
        warnings.append(
            f"{len(parsed) - len(importable)} memory entries were left out: they are "
            "scoped to users this platform does not have."
        )
    if malformed:
        warnings.append(
            f"{malformed} memory entries were left out: they could not be read "
            "or cannot be imported (empty, tradecraft)."
        )
    created = await import_memories(db, agent_id=agent_id, entries=importable)
    return len(created)


async def create_agent_from_bundle(
    db: AsyncSession,
    *,
    user: User,
    bundle: dict[str, Any],
    request: Request | None = None,
    source_format: str = "native",
    audit_details: dict[str, Any] | None = None,
) -> ImportedAgent:
    """Create an agent (plus its tools, servers, skills, prompts, assignments).

    Everything is created as private entities owned by ``user``. Integration
    bindings and knowledge bases are deliberately not imported: they are
    platform-specific, and a binding to another instance's integration would
    dangle. An assignment trigger keeps the integration it names only when
    ``user`` can see it here, otherwise it is removed with a warning.

    Does not commit — the caller owns the transaction boundary so an import can
    be rolled back as a unit.

    ``source_format`` and ``audit_details`` only shape the audit-log entry; the
    creation logic is identical for every format by design.
    """
    agent_data = _require_importable(bundle)
    from app.services.llm_provider_deletion import (
        AGENT_MODEL_FIELDS,
        lock_provider_configs,
    )

    # The bundle's model choice is serialized with provider deletions like an
    # agent save's, before this import writes anything - a provider
    # named without a model included, since a deletion resets that one by name.
    if any(
        agent_data.get(field)
        for field in AGENT_MODEL_FIELDS
        if field in AGENT_BUNDLE_PORTABLE_FIELDS
    ):
        await lock_provider_configs(db, shared=True)
    warnings: list[str] = []

    # --- Variables (v3 bundles) ---
    # Created first: every text field imported below is rewritten to point at
    # the new variable ids.
    var_id_map: dict[str, str] = {}
    for v_data in bundle.get("variables") or []:
        if not isinstance(v_data, dict):
            continue
        old_id = v_data.get("id")
        if not old_id:
            continue
        var = Variable(
            user_id=user.id,
            name=_variable_name(v_data)[1] or "IMPORTED_VAR",
            value=v_data.get("value") if not v_data.get("is_secret") else None,
            is_secret=v_data.get("is_secret", False),
            description=v_data.get("description"),
            tags=v_data.get("tags", []),
            is_company_managed=False,
        )
        db.add(var)
        await db.flush()
        var_id_map[str(old_id).lower()] = str(var.id)

    def _rewrite(text: str | None) -> str | None:
        if not text or not var_id_map:
            return text
        return rewrite_variable_ids(text, var_id_map)

    # --- Custom Tools ---
    new_tool_ids: list[str] = []
    for ct_data in bundle.get("custom_tools") or []:
        if not isinstance(ct_data, dict):
            continue
        # Absent in older bundles (a Python tool then imported as JavaScript);
        # anything but the two supported languages / a positive integer keeps
        # the model default.
        _ct_language = ct_data.get("language")
        _ct_timeout = ct_data.get("timeout")
        tool = CustomTool(
            user_id=user.id,
            name=_entry_text(ct_data, "name", "Imported Tool", section="custom tool"),
            description=_rewrite(ct_data.get("description", "")),
            icon_url=ct_data.get("icon_url"),
            input_schema=ct_data.get("input_schema"),
            js_function=_rewrite(ct_data.get("js_function")),
            language=(
                _ct_language if _ct_language in ("javascript", "python") else "javascript"
            ),
            timeout=(
                _ct_timeout
                if isinstance(_ct_timeout, int)
                and not isinstance(_ct_timeout, bool)
                and _ct_timeout > 0
                else 300
            ),
            tags=ct_data.get("tags", []),
            is_company_managed=False,
            enabled=True,
        )
        db.add(tool)
        await db.flush()
        new_tool_ids.append(str(tool.id))

    # --- MCP Servers ---
    new_mcp_ids: list[str] = []
    new_mcp_allowed_tools: dict[str, list[str]] = {}
    # Bundle-local server ``ref`` -> id of the server recreated here, for the
    # assignments' ``tool_sources``.
    new_mcp_id_by_ref: dict[str, str] = {}
    for srv_data in bundle.get("mcp_servers") or []:
        if not isinstance(srv_data, dict):
            continue
        # The bundle-local key an assignment's ``tool_sources`` names this
        # server by; a missing or null ``ref`` leaves it unnamed.
        srv_ref = _entry_text(srv_data, "ref", "", section="MCP server")
        srv_transport = srv_data.get("transport", "stdio")
        srv_url = srv_data.get("url")  # a string or null: the bundle's validation
        # A server the bundle names is one this platform will connect to: the
        # MCP server URL policy of the form applies.
        await require_allowed_mcp_server_url(srv_url, transport=srv_transport, routed=False)
        srv = MCPServer(
            user_id=user.id,
            name=_entry_text(srv_data, "name", "Imported Server", section="MCP server"),
            transport=srv_transport,
            command=srv_data.get("command"),
            args=srv_data.get("args", []),
            url=srv_url,
            tags=srv_data.get("tags", []),
            is_company_managed=False,
            enabled=True,
        )
        db.add(srv)
        await db.flush()
        new_mcp_ids.append(str(srv.id))
        if srv_ref:
            new_mcp_id_by_ref.setdefault(srv_ref, str(srv.id))
        # Re-key the per-server tool scope onto the freshly created id (the
        # exported map was keyed by the SOURCE instance's server id, which
        # resolves to nothing here). Absent / null means "every tool", which is
        # also what an older bundle produces.
        _srv_allowed = srv_data.get("allowed_tools")
        if isinstance(_srv_allowed, list):
            new_mcp_allowed_tools[str(srv.id)] = [str(t) for t in _srv_allowed]

    # --- Skills (v2 bundles) ---
    new_skill_ids: list[str] = []
    new_skill_load_modes: dict[str, str] = {}
    for sk_data in bundle.get("skills") or []:
        if not isinstance(sk_data, dict):
            continue
        skill = AgentSkill(
            user_id=user.id,
            name=_entry_text(sk_data, "name", "Imported Skill", section="skill"),
            description=sk_data.get("description"),
            url=sk_data.get("url"),
            content=_rewrite(sk_data.get("content")),
            compatibility=sk_data.get("compatibility"),
            license=sk_data.get("license"),
            tags=sk_data.get("tags", []),
            is_company_managed=False,
            enabled=True,
        )
        db.add(skill)
        await db.flush()
        new_skill_ids.append(str(skill.id))
        # Re-key the exported per-skill load mode onto the freshly created id.
        # Unknown / missing values degrade to progressive (the default), which
        # is also what an older bundle produces.
        if str(sk_data.get("load_mode") or "").strip().lower() == "always":
            new_skill_load_modes[str(skill.id)] = "always"

    # --- Prompts (v2 bundles) ---
    new_prompt_ids: list[str] = []
    for pr_data in bundle.get("prompts") or []:
        if not isinstance(pr_data, dict):
            continue
        prompt = Prompt(
            user_id=user.id,
            name=_entry_text(pr_data, "name", "Imported Prompt", section="prompt"),
            description=pr_data.get("description"),
            content=_rewrite(pr_data.get("content", "")),
            tags=pr_data.get("tags", []),
            is_company_managed=False,
        )
        db.add(prompt)
        await db.flush()
        new_prompt_ids.append(str(prompt.id))

    # --- Agent ---
    agent_name = agent_data["name"]
    agent_slug = await generate_unique_agent_slug(db, agent_name)

    # Sanitize the imported model override instead of rejecting the whole
    # bundle: export bundles come from OTHER instances whose model catalog
    # can differ from ours, and a hard failure over a model id would make
    # cross-instance sharing needlessly brittle. An unlisted model is
    # dropped (-> platform default) with an INFO breadcrumb; everything
    # else about the agent imports normally.
    imported_llm_provider = agent_data.get("llm_provider")
    imported_llm_model = agent_data.get("llm_model")
    # A bundle is untrusted: an unknown source reads as none. On the
    # routing model the agent keeps no model of its own: one carried alongside
    # would come back as a stale pin the day the source is cleared.
    imported_llm_model_source = normalize_llm_model_source(
        agent_data.get("llm_model_source")
    )
    if imported_llm_model_source == LLM_MODEL_SOURCE_ROUTING:
        imported_llm_provider = None
        imported_llm_model = None
    if imported_llm_model and isinstance(imported_llm_model, str):
        from app.llm.router import validate_selectable_chat_model

        # A non-admin keeps a model only one of their providers offers.
        model_error = await validate_selectable_chat_model(
            db, imported_llm_model, user_id=user.id, scoped=not user.is_admin
        )
        if model_error:
            logger.info(
                "Agent import: dropping model override %r (not in the "
                "available_models of a provider the importer may use); the "
                "imported agent will use the platform default model.",
                imported_llm_model,
            )
            warnings.append(
                f"The model '{imported_llm_model}' is not available to you on "
                "this platform; the imported agent uses the default model."
            )
            imported_llm_model = None
            imported_llm_provider = None
    # A model or provider that its column cannot hold is not one this platform
    # can store: both are dropped like an unlisted model rather than failing
    # the INSERT (the catalog check above only reads text, and is skipped while
    # no provider lists a model).
    from app.core.utils import text_column_error

    unstorable = next(
        (
            error
            for key, value in (("llm_provider", imported_llm_provider), ("llm_model", imported_llm_model))
            if (error := text_column_error(Agent.__table__.c[key], value))
        ),
        None,
    )
    if unstorable is not None:
        warnings.append(
            f"The model override was not imported: {unstorable} The imported "
            "agent uses the default model."
        )
        imported_llm_model = None
        imported_llm_provider = None

    # Which fields an imported agent inherits is decided by the shared bundle
    # manifest in ``app.models.agent_copy``, not by a kwargs list maintained
    # here — the hand-written list is what silently dropped
    # ``integration_template_bindings`` (and with it every per-user OpenCTI tool)
    # from every re-imported agent. Only the fields that need local
    # validation, a variable-id rewrite, or re-keying onto rows created above
    # are spelled out as overrides.
    agent = Agent(
        **build_agent_import_kwargs(
            agent_data,
            user_id=user.id,
            is_company_managed=False,
            name=agent_name,
            slug=agent_slug,
            persona=_rewrite(agent_data.get("persona")),
            guardrails=_rewrite(agent_data.get("guardrails")),
            llm_provider=imported_llm_provider,
            llm_model=imported_llm_model,
            llm_model_source=imported_llm_model_source,
            # Clamp the agent-level value from the imported bundle to the shared
            # ceiling (null = inherit -> platform default), same as the REST paths.
            max_iterations=(
                clamp_max_iterations(_agent_mi)
                if (_agent_mi := agent_data.get("max_iterations")) is not None
                else None
            ),
            # Same for the run budget: a document is written elsewhere,
            # so importing one must not be a way to hold a budget above the
            # platform ceiling. Lowered rather than refused — the importer did
            # not choose the number.
            budget_config=lower_budget_to_ceiling(
                agent_data.get("budget_config"), user=user
            ),
            builtin_tool_ids=_resolve_builtin_tool_ids(agent_data),
            # A per-integration tool scope names an integration of the source
            # install: folded into its provider's list, read per integration
            # once the new owner binds theirs (never silently widened).
            integration_allowed_tools=fold_unbound_instance_scopes(
                agent_data.get("integration_allowed_tools"), set()
            ),
            # A bundle is untrusted: only the flags of enforceable types the
            # imported selection has are kept.
            integration_element_enforcement=normalize_scope_enforcement(
                agent_data.get("integration_element_enforcement"),
                agent_data.get("integration_element_bindings"),
            ),
            # The scopes, never the agents they list (they are the exporting
            # instance's); an unknown scope reads as the narrowest.
            coordination_access=portable_coordination_access(
                agent_data.get("coordination_access")
            ),
            # Re-keyed onto the tools / servers / skills / prompts created above.
            custom_tool_ids=new_tool_ids,
            mcp_server_ids=new_mcp_ids,
            mcp_allowed_tools=new_mcp_allowed_tools or None,
            skill_ids=new_skill_ids,
            skill_load_modes=new_skill_load_modes or None,
            prompt_ids=new_prompt_ids,
            # Cleared deliberately: every one of these holds a UUID that only
            # resolves on the exporting instance. See AGENT_BUNDLE_SKIP_FIELDS /
            # AGENT_BUNDLE_EXPORT_ONLY_FIELDS for the per-field reasons.
            available_channel_types=["web"],
            integration_bindings={},
            knowledge_base_ids=[],
            handover_agent_ids=[],
            work_item_owner_agent_ids=[],
            trace_supervision_target_agent_ids=[],
        )
    )
    db.add(agent)
    await db.flush()

    # Element scopes are carried (fail-closed: dropping them would widen the
    # agent's reach once the user connects their own integration), but they name
    # repositories / channels of the source platform, so say so instead of
    # letting the agent quietly match nothing.
    if agent.integration_element_bindings:
        warnings.append(
            "This agent is scoped to specific integration elements "
            "(repositories, channels, ...) chosen on the source platform. "
            "Review them in Config > Integrations: an element that does not "
            "exist here matches nothing."
        )

    # Same "Initial version" baseline a created or duplicated agent gets.
    await _seed_initial_versions(db, agent=agent, author_id=user.id)

    # --- Assignments ---
    # Reject invalid cron expressions at import time — same contract as
    # the assignment POST/PUT schemas. Without this, an exported bundle
    # could carry a malformed cron that silently crashes the scheduler
    # on every tick after import. Imported assignments
    # already start disabled, but a future enable click would re-trigger
    # the crash.
    visible_server_ids = await _visible_mcp_server_ids(
        db,
        user,
        _referenced_server_ids(bundle.get("assignments"), new_mcp_id_by_ref),
    )
    visible_trigger_integration_ids = await _visible_integration_ids(
        db,
        user,
        _referenced_trigger_integration_ids(bundle.get("assignments")),
    )
    wf_count = 0
    for wf_data in bundle.get("assignments") or []:
        if not isinstance(wf_data, dict):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Imported assignments must be objects, got "
                    f"{type(wf_data).__name__}."
                ),
            )
        wf_name = _entry_text(wf_data, "name", "Imported Assignment", section="assignment")
        wf_trigger_type = _entry_text(wf_data, "trigger_type", "manual", section="assignment")
        wf_prompt = _entry_text(wf_data, "prompt", "", section="assignment")
        # ``trigger_config`` is typed as ``Mapped[dict]`` on the model but
        # the JSON column accepts any JSON value at the storage layer.
        # A bundle authored elsewhere could carry a non-dict value
        # (string / list / scalar) that would silently persist and then
        # crash anywhere downstream that does ``trigger_config.get(...)``
        # — most notably the scheduler when the trigger type is later
        # flipped to ``"schedule"``. Reject up-front with the same 400
        # shape as the cron validator below so the error surfaces against
        # the specific offending assignment instead of being deferred to
        # a runtime crash. Mirrors the schema validator's "must be an
        # object" contract on the HTTP create/update path.
        #
        # IMPORTANT — do NOT use ``.get(..., {}) or {}`` here. That
        # shortcut silently coerces *falsy* non-dict JSON values
        # (``[]``, ``""``, ``0``, ``False``) to ``{}`` *before* the
        # ``isinstance(..., dict)`` check fires, so those payloads
        # would slip past the intended 400 and persist as an empty
        # dict (data loss). Default ``None``/missing to ``{}`` first
        # (those are the only shapes that mean "no trigger_config"),
        # then run the type check on everything else.
        wf_trigger_config_raw = wf_data.get("trigger_config")
        if wf_trigger_config_raw is None:
            wf_trigger_config_raw = {}
        elif not isinstance(wf_trigger_config_raw, dict):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Imported assignment '{wf_name}' has an "
                    f"invalid trigger_config: must be an object, got "
                    f"{type(wf_trigger_config_raw).__name__}."
                ),
            )
        wf_trigger_config = wf_trigger_config_raw
        if wf_trigger_type == "schedule":
            try:
                validate_schedule_trigger_config(wf_trigger_type, wf_trigger_config)
            except ValueError as cron_err:
                # The schedule rule the API and the agent builder apply; its
                # message is already ``Invalid cron expression '...': ...`` —
                # surface it verbatim so the API doesn't double-print the prefix.
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Imported assignment '{wf_name}': "
                        f"{cron_err}"
                    ),
                ) from cron_err
        wf_trigger_config, dropped_trigger_integration = _importable_trigger_config(
            wf_trigger_config, visible_trigger_integration_ids
        )
        if dropped_trigger_integration:
            warnings.append(_dropped_trigger_integration_warning(wf_name))
        wf_tool_sources, dropped_sources = resolve_bundle_tool_sources(
            _bundle_tool_sources(wf_data, name=wf_name),
            id_by_ref=new_mcp_id_by_ref,
            visible_ids=visible_server_ids,
        )
        if dropped_sources:
            warnings.append(
                _dropped_tool_sources_warning(
                    wf_name,
                    dropped_sources,
                    wf_tool_sources,
                )
            )
        wf_integration_bindings, integration_note = import_integration_override(
            wf_data, str(wf_data.get("name") or "Imported Assignment")
        )
        if integration_note:
            warnings.append(integration_note)
        wf = Assignment(
            agent_id=agent.id,
            user_id=user.id,
            name=wf_name,
            description=wf_data.get("description"),
            trigger_type=wf_trigger_type,
            # Exported bundles never carry webhook secrets — give imported
            # webhook assignments a fresh one so they are born with
            # signature verification material like API-created ones (SEC-5).
            trigger_config=ensure_webhook_secret(wf_trigger_type, wf_trigger_config),
            activation_prompt=_rewrite(wf_data.get("activation_prompt")),
            # A malformed check from a hand-edited bundle is dropped, never
            # stored: the engine would only skip it at run time.
            activation_check=stored_activation_check(wf_data.get("activation_check")),
            prompt=_rewrite(wf_prompt),
            delay=wf_data.get("delay"),
            tool_sources=wf_tool_sources,
            integration_bindings=wf_integration_bindings,
            use_knowledge_bases=wf_data.get("use_knowledge_bases", True),
            ai_label=wf_data.get("ai_label", True),
            ai_label_prompt=wf_data.get("ai_label_prompt"),
            notify_on_complete=wf_data.get("notify_on_complete", False),
            show_in_digest=wf_data.get("show_in_digest", False),
            human_in_the_loop=wf_data.get("human_in_the_loop", True),
            human_in_the_loop_prompt=wf_data.get("human_in_the_loop_prompt"),
            # null = inherit (agent override -> platform default). Clamp a real
            # value to the shared ceiling so an imported bundle can't smuggle an
            # out-of-range max_iterations past the same invariant the REST
            # create/update paths enforce.
            max_iterations=(
                clamp_max_iterations(_wf_mi)
                if (_wf_mi := wf_data.get("max_iterations")) is not None
                else None
            ),
            timeout_seconds=wf_data.get("timeout_seconds", 300),
            retry_on_failure=wf_data.get("retry_on_failure", False),
            max_retries=wf_data.get("max_retries", 3),
            dry_run=wf_data.get("dry_run", False),
            # Narrowing fields: the self-trigger loop guard and the tool scopes
            # (provider-keyed, portable). Absent in older bundles -> defaults.
            ignore_self_triggered_events=bool(
                wf_data.get("ignore_self_triggered_events", False)
            ),
            integration_allowed_tools=fold_unbound_instance_scopes(
                wf_data.get("integration_allowed_tools"), set()
            ),
            integration_element_bindings=wf_data.get("integration_element_bindings"),
            # Only the flags of enforceable types the imported selection has.
            integration_element_enforcement=normalize_scope_enforcement(
                wf_data.get("integration_element_enforcement"),
                wf_data.get("integration_element_bindings"),
            ),
            hide_runs=wf_data.get("hide_runs"),
            manual_trigger_parameters=wf_data.get("manual_trigger_parameters"),
            # Tri-state runner refinement preserved as-is (None = inherit the
            # agent's runners; [] = none; subset = refine). ``.get`` keeps the
            # None-vs-[] distinction that ``or []`` would flatten.
            runner_ids=wf_data.get("runner_ids"),
            enabled=False,  # Imported assignments start disabled for safety
        )
        # Explain the "Paused" badge on a freshly imported assignment.
        pause_assignment(
            wf,
            reason="created_disabled",
            details=(
                "Created switched off: imported from a file. Review the "
                "configuration, then enable it."
            ),
            actor_user_id=user.id,
            actor_label=actor_display_name(user),
        )
        db.add(wf)
        wf_count += 1

    memory_count = await _import_bundle_memories(
        db, agent_id=agent.id, entries=bundle.get("memories"), warnings=warnings
    )

    await log_audit(
        db,
        user=user,
        action="import",
        entity_type="agent",
        entity_id=agent.id,
        entity_name=agent.name,
        summary=f"Imported agent '{agent.name}'",
        details={
            "slug": agent.slug,
            "assignment_count": wf_count,
            "memory_count": memory_count,
            "llm_model": agent.llm_model,
            "tags": agent.tags or [],
            # Which format the file was in, for the import audit trail and the
            # adoption telemetry that counts imports by detected format.
            "source_format": source_format,
            **(audit_details or {}),
        },
        request=request,
    )
    return ImportedAgent(agent=agent, assignment_count=wf_count, warnings=warnings)
