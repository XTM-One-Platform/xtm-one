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

"""The agent builder tool (agent/assignment/entity CRUD from chat).

Moved verbatim from the original ``app/services/builtin_tools.py`` module
as part of the package split (mechanical refactor - no behavior change).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import re
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError

from app.api.v1.helpers import (
    agent_tool_admin_bypass,
    package_origin_custom_tool_error,
    package_origin_knowledge_base_error,
    source_managed_custom_tool_error,
    workspace_managed_knowledge_base_error,
    writable_agent_error,
    writable_assignment_error,
    writable_knowledge_base_error,
)
from app.core.feature_flags import FeatureFlag, is_feature_enabled
from app.core.lone_surrogates import lone_surrogate_error
from app.core.security_options import security_option
from app.core.utils import (
    MCP_SERVER_URL_LABELS,
    MaskedArgumentError,
    format_exception_message,
    is_statement_timeout,
    redact_secret_text,
    restore_masked_args,
    restore_masked_text,
    safe_uuid,
    schedule_trigger_error,
)
from app.models.assignment import new_trigger_config
from app.models.general_assistant_config import MAX_ITERATIONS_CEILING
from app.services.assignment_activation_check import describe_check as _describe_gate_check
from app.services.builtin_tool_aliases import (
    BUILTIN_GROUP_ALIASES,
    RENAMED_BUILTIN_GROUPS,
    alias_group,
    current_group_id,
    describe_builtin_tool_id,
    is_former_builtin_id,
)
from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS
from app.services.element_scope import ScopeEnforcementError, parse_scope_enforcement
from app.services.handover_types import is_legacy_handover_map
from app.services.integration_auth_guard import UNREADABLE_STOP_REASON, auth_failures_line
from app.services.integration_credentials import (
    PIPELINE_TOOL_REF_FIELDS,
    added_integration_bindings,
    flatten_binding_ids,
    pipeline_refs_refusal,
)
from app.services.vault import (
    holds_any_reference,
    reference_problem,
    retained_reference_writer_problem,
    secret_fields_problem,
    secret_fields_writer_problem,
    variable_reference_problem,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import _bounded_display, _platform_label, logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent
    from app.tools.base import FileContext

# ── Agent Builder ─────────────────────────────────────────────────────────────


#: The update_agent arguments create_agent has no use for: the agent to edit,
#: and how a binding field merges with the agent's current set.
UPDATE_AGENT_ONLY_FIELDS = frozenset(
    {"agent_id", "binding_mode", "mode", "field", "edits", "section", "content"}
)

#: The text fields ``update_agent`` edits partially (mode edit, append,
#: replace_section, insert_after), the first the default.
AGENT_PARTIAL_TEXT_FIELDS = ("persona", "guardrails", "description")
#: The text fields ``update_assignment`` edits partially, the first the default.
ASSIGNMENT_PARTIAL_TEXT_FIELDS = ("prompt", "activation_prompt", "description")


def _partial_text_schema(fields: tuple[str, ...]) -> dict[str, dict[str, object]]:
    """The schema properties of a partial text edit (``update_skill``'s modes) on one of *fields*."""
    return {
        "mode": {
            "type": "string",
            "enum": ["replace", "edit", "append", "replace_section", "insert_after"],
            "description": (
                "'replace' (default): the field passed whole. Else 'field' changes in place: "
                "'edit' applies 'edits'; 'append' adds 'content'; 'replace_section' / "
                "'insert_after' replace / follow 'section' with 'content'. Answers the diff."
            ),
            "default": "replace",
        },
        "field": {
            "type": "string",
            "enum": list(fields),
            "description": f"The text field a partial mode changes (default '{fields[0]}').",
            "default": fields[0],
        },
        "edits": {
            "type": "array",
            "description": (
                "For mode 'edit': ordered exact find/replace operations on the stored text; "
                "each find must exist and be unique (or set replace_all); nothing is written "
                "unless every edit applies."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["find", "replace"],
            },
        },
        "section": {
            "type": "string",
            "description": (
                "For 'replace_section' and 'insert_after': the Markdown section, by heading "
                "text, heading path 'Parent > Child' or outline index '#3'."
            ),
            "default": "",
        },
        "content": {
            "type": "string",
            "description": (
                "For 'append', 'replace_section' and 'insert_after': the text to add; a whole "
                "section, heading line included, for the section modes."
            ),
            "default": "",
        },
    }


#: How long one ``run_agent_once`` call waits for the run's answer: well inside
#: the loop's per-tool timeout, after which the call returns the task to collect.
_RUN_ONCE_WAIT_SECONDS = 60.0

#: The ``activation_check`` argument of create_assignment / update_assignment
#: (``services/assignment_activation_check.py`` parses it).
_ACTIVATION_CHECK_SCHEMA: dict = {
    "type": "string",
    "description": (
        "Optional JSON gate, NO model call, before activation_prompt: "
        "a READ-ONLY tool the agent holds and a condition on its result, "
        'e.g. {"tool": "list_work_items", "arguments": '
        '{"owner_agent_id": "{{agent.id}}", "open_only": true}, "condition": "non_empty"}. '
        'condition: "non_empty" (default), "empty", or {"path": "$.status", "in" | "not_in" | '
        '"none_in" | "contains": ...}. Failed: skipped at zero tokens; unevaluable: run '
        "proceeds. Placeholders: {{agent.id}}, {{assignment.id}}, {{payload.a.b}}, "
        "{{date.today+N}}, {{datetime.day_start+N}}, {{datetime.day_end+N}} (trigger zone). "
        "One integration's tool: base_name@integration_id. also/or: 3 more, all/any pass. "
        "Update: 'none' removes it, omit keeps it."
    ),
    "default": "",
}

#: Commit message of the versions an ``update_agent`` call records, so the
#: Versioning tab says where the edit came from.
BUILDER_EDIT_COMMIT_MESSAGE = "Edited through the agent builder"

#: Set while ``create_agent`` configures the agent it just created through the
#: update path: the versions recorded then are the agent's initial ones, with
#: no baseline of the creation defaults. A context variable, not an argument,
#: because ``update_agent`` is the tool's handler and the model must not set it.
_CONFIGURING_NEW_AGENT: ContextVar[bool] = ContextVar(
    "agent_builder_configuring_new_agent", default=False
)


class _AgentUpdated(str):
    """The result of a successful ``update_agent``, with the fields it wrote.

    A ``str`` the model reads unchanged; ``create_agent`` and ``clone_agent``
    run the update path for the fields they share with it and tell a success
    from a refusal by type, never by parsing the wording.
    """

    fields: tuple[str, ...]

    def __new__(cls, text: str, *, fields: tuple[str, ...] = ()) -> _AgentUpdated:
        result = super().__new__(cls, text)
        result.fields = fields
        return result


class _AgentDeleted(str):
    """The result of a successful ``delete_agent``, told from a refusal by type."""


def _variable_value_problem(value: str | None, is_secret: bool) -> str | None:
    """The REST rule: a vault reference is the value of a secret variable only."""
    if not value:
        return None
    if is_secret:
        return reference_problem(value)
    return "a vault reference can only be the value of a secret variable" if holds_any_reference(value) else None


async def _vault_writer_refusal(uid: uuid.UUID, fields: dict[str, Any], _db: AsyncSession | None = None) -> str | None:
    """The REST rule: a vault reference is written only by a caller its provider admits at that path.

    The caller's role and grants decide (an environment provider's
    ``WRITERS``, a database provider's grants), as in a REST write: the
    provider's policy is about who may point a stored secret at the company
    vault, not about what the agent can see.
    """
    if not any(holds_any_reference(value) for value in fields.values()):
        return None
    from app.services.vault.store import vault_principal

    if _db is None:
        from app.core.database import async_session

        async with async_session() as session:
            caller = await _load_caller_user_for_builder(session, uid)
            principal = await vault_principal(session, caller)
    else:
        caller = await _load_caller_user_for_builder(_db, uid)
        principal = await vault_principal(_db, caller)
    return secret_fields_writer_problem(fields, principal=principal)


async def _retained_reference_refusal(
    entity: Any, uid: uuid.UUID, _db: AsyncSession, *, repoints: bool, replaced: set[str]
) -> str | None:
    """The REST rule: an edit re-pointing an entity that keeps a reference its caller could not write is refused.

    *repoints*: the edit changes where the entity sends its secrets; *replaced*:
    the reference columns it rewrites (``retained_reference_writer_problem``).
    """
    if not repoints:
        return None
    from app.services.vault.config import VaultPrincipal
    from app.services.vault.store import vault_principal

    if retained_reference_writer_problem(entity, replaced=replaced, principal=VaultPrincipal(is_admin=False)) is None:
        return None
    caller = await _load_caller_user_for_builder(_db, uid)
    return retained_reference_writer_problem(entity, replaced=replaced, principal=await vault_principal(_db, caller))


def _mcp_args(args: str) -> list[str]:
    """MCP server arguments given to a builder tool: a JSON list, or comma-separated."""
    try:
        parsed = json.loads(args)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(a) for a in parsed]
    return [a.strip() for a in args.split(",") if a.strip()]


def _as_sentence(text: str) -> str:
    """End a shared refusal with a period, as the builder's tool results do."""
    return text if text.endswith(".") else f"{text}."


#: The ``tool_slug`` argument of create_integration / update_integration.
_TOOL_SLUG_SCHEMA: dict = {
    "type": "string",
    "description": (
        "Tool-name suffix used when an agent binds several integrations of the "
        "provider (prod: list_issues__prod). Lowercase letters, digits, underscores, "
        "at most 21 characters, unique per provider and owner. Empty leaves it unchanged."
    ),
    "default": "",
}


def _tool_slug_argument(value: object) -> tuple[str | None, str | None]:
    """The ``tool_slug`` a builder tool was given, normalized, or why it is refused.

    Blank (or ``null``) means "leave it": ``(None, None)``. Otherwise the REST
    rule (``tool_slug_error``: lowercase pattern, length, no edge underscore)
    on the stripped value, checked before any read or write; the
    uniqueness per provider and owner scope is ``validate_tool_slug_change``'s.
    """
    from app.core.integration_tool_slug import tool_slug_error

    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if not isinstance(value, str):
        return None, "tool_slug must be a string."
    slug = value.strip()
    if error := tool_slug_error(slug):
        return None, error
    return slug, None


#: How create_integration / update_integration describe the per-user opt-out,
#: in the Integrations UI's words. Kept short: every character counts toward
#: the built-in tool schema ceiling (tests/test_builtin_tool_groups.py).
_SHARE_CONNECTED_ACCOUNT_RULE = (
    " share_connected_account false = \"Don't share my connected account\" "
    "(per_user): others connect their own; the owner's agents keep it in "
    "assignments and flows."
)

#: The ``share_connected_account`` / ``credential_mode`` arguments; the
#: tool description carries their meaning once.
_SHARE_CONNECTED_ACCOUNT_SCHEMA: dict = {"type": "boolean"}
_CREDENTIAL_MODE_SCHEMA: dict = {"type": "string", "enum": ["shared", "per_user"]}
#: Which way an A2A integration points (A2A providers only; inbound by default on create).
_A2A_DIRECTION_SCHEMA: dict[str, object] = {"type": "string", "enum": ["inbound", "outbound"]}


def _share_connected_account_argument(
    share_connected_account: object, credential_mode: object
) -> tuple[bool | None, str | None]:
    """The connected-account choice a builder tool was given, or why it is refused.

    ``share_connected_account`` (a boolean, or its ``\"true\"`` / ``\"false\"``
    string) and its alias ``credential_mode`` (``\"shared\"`` / ``\"per_user\"``)
    map to the ``share_credentials`` of ``apply_oauth_share_consent``; both
    omitted (``None`` or blank) is ``(None, None)``: no choice.
    """
    from app.services.integration_credentials import (
        PER_USER_CREDENTIAL_MODE,
        SHARED_CREDENTIAL_MODE,
    )

    choice: bool | None = None
    if isinstance(share_connected_account, bool):
        choice = share_connected_account
    elif isinstance(share_connected_account, str) and share_connected_account.strip():
        normalized = share_connected_account.strip().lower()
        if normalized not in ("true", "false"):
            return None, "share_connected_account must be true or false."
        choice = normalized == "true"
    elif share_connected_account is not None and not isinstance(share_connected_account, str):
        return None, "share_connected_account must be true or false."

    mode_choice: bool | None = None
    if isinstance(credential_mode, str) and credential_mode.strip():
        mode = credential_mode.strip().lower()
        if mode not in (SHARED_CREDENTIAL_MODE, PER_USER_CREDENTIAL_MODE):
            return None, "credential_mode must be 'shared' or 'per_user'."
        mode_choice = mode == SHARED_CREDENTIAL_MODE
    elif credential_mode is not None and not isinstance(credential_mode, str):
        return None, "credential_mode must be 'shared' or 'per_user'."

    if choice is not None and mode_choice is not None and choice != mode_choice:
        return None, (
            "share_connected_account and credential_mode disagree: give one of them."
        )
    return (choice if choice is not None else mode_choice), None


def _oauth_scope_lines(integration: Any) -> list[str]:
    """get_integration's scope lines: requested, granted by the stored token, missing.

    A scope the person declined at consent is the usual cause of a 403 right
    after a reconnect; "Missing" names it, with the fix (reconnect and tick it).
    """
    from app.api.v1.integration_catalog import PROVIDER_CATALOG
    from app.integrations.granted_scopes import missing_scopes, split_scopes

    catalog = PROVIDER_CATALOG.get(getattr(integration, "provider", "") or "", {})
    requested = getattr(integration, "scopes", None) or catalog.get("scopes")
    lines = [f"  • Requested scopes: {' '.join(split_scopes(requested)) or 'None'}"]
    granted = getattr(integration, "granted_scopes", None) if integration.access_token_enc else None
    if granted is None:
        lines.append("  • Granted scopes: unknown (the provider did not report them, or not connected)")
        return lines
    lines.append(f"  • Granted scopes: {' '.join(split_scopes(granted)) or 'None'}")
    missing = missing_scopes(requested, granted, catalog.get("optional_scopes")) or []
    lines.append(
        f"  • Missing scopes: {' '.join(missing)} (reconnect the integration and grant them)"
        if missing
        else "  • Missing scopes: None"
    )
    return lines


def _credential_mode_line(integration: Any, connected_people: int | None = None) -> str:
    """The credential mode of an OAuth integration, for a tool's answer.

    Empty for a row that is not OAuth. On a ``per_user`` row, how many people
    connected their own account when *connected_people* is known: a count,
    never who.
    """
    from app.services.integration_credentials import PER_USER_CREDENTIAL_MODE

    if getattr(integration, "auth_type", None) != "oauth":
        return ""
    if getattr(integration, "credential_mode", None) == PER_USER_CREDENTIAL_MODE:
        line = (
            "per_user (the connected account stays its owner's; everyone else "
            "it is shared with authorizes their own"
        )
        if connected_people is not None:
            line += f"; {connected_people} connected their own account"
        return line + ")"
    return "shared (everyone it is shared with acts with the connected account)"


def _unapplied_share_choice_note(integration: Any, share_choice: bool) -> str:
    """Why a connected-account choice left the row as it is, or ``""``.

    ``apply_oauth_share_consent`` keeps the choice only on a shared OAuth
    row: anything else has no account to lend, and is personal again.
    """
    from app.services.integration_credentials import is_shared_integration

    if getattr(integration, "auth_type", None) != "oauth":
        return (
            "\n  • share_connected_account applies to OAuth integrations only: "
            "nothing changed."
        )
    if not is_shared_integration(integration):
        return (
            "\n  • The integration is not shared, so its connected account is its "
            "owner's only: give share_connected_account again with the share "
            "(shared_group_ids) to choose how it is shared."
        )
    return ""


async def _load_caller_user_for_builder(_db: AsyncSession, uid: uuid.UUID):
    """Load the caller's ``User`` row.

    Module-level helper (rather than a closure) so tests can monkeypatch
    it and the agent-builder gates can be exercised in isolation.
    """
    from app.models.user import User

    result = await _db.execute(sa_select(User).where(User.id == uid))
    return result.scalar_one_or_none()


_admin_write_bypass_reported: list[bool] = []


def _report_admin_write_bypass(kind: str, entity_id: uuid.UUID) -> None:
    """Say once per process that an admin's agent is writing past its three tiers."""
    if _admin_write_bypass_reported:
        return
    _admin_write_bypass_reported.append(True)
    from app.core.security_options import where_to_set

    logger.warning(
        "Agent builder: an admin's agent reached %s %s outside the admin's own, "
        "group-shared and company-managed objects, to write it through the "
        "admin bypass these tools share with the REST API. %s to keep admins' "
        "agents to their three tiers.",
        kind,
        entity_id,
        where_to_set("agent_tools_strict_admin_access", "true", sentence_start=True),
    )


def _trigger_refusal_message(refusal, assignment, agent) -> str:
    """Word a shared ``ManualTriggerRefusal`` for the ``trigger_assignment`` tool."""
    active = refusal.active_run
    wait_for = (
        f" {active.describe()} Wait for that run (its id is what `until` takes) "
        "instead of triggering it again."
        if active is not None
        else " Wait for it to finish before triggering it again."
    )
    return {
        "assignment_disabled": (
            f"Assignment **{assignment.name}** is disabled. Enable it first."
        ),
        "agent_disabled": f"Agent **{agent.name}** is disabled. Enable it first.",
        "admin_only": f"{refusal.detail}.",
        "own_run_active": (
            "You already have a run in progress for assignment "
            f"**{assignment.name}**.{wait_for}"
        ),
        "run_active": (
            f"Assignment **{assignment.name}** already has an active run.{wait_for}"
        ),
    }.get(refusal.reason, f"{refusal.detail}.")


async def _validate_selectable_chat_model_for_builder(
    _db: AsyncSession,
    model_id: str,
    *,
    user_id: uuid.UUID | None = None,
    stored_model: str | None = None,
) -> str | None:
    """Thin, monkeypatch-friendly wrapper around the shared model gate.

    Module-level (rather than an inline import inside the tool closure) so
    tests can stub it on the ``builtin_tools`` facade, mirroring the
    ``_load_writable_agent_for_builder`` pattern. The gate itself lives in
    ``app.llm.router.validate_selectable_chat_model`` — single source of
    truth shared with the REST API and settings endpoints.

    A model the caller picks must be offered by a provider they may use
    unless their agent takes the admin bypass; the model the agent
    already stores is not their pick and is never re-judged.
    """
    from app.llm.router import validate_selectable_chat_model

    scoped = False
    if user_id is not None and model_id != stored_model:
        scoped = not agent_tool_admin_bypass(await _load_caller_user_for_builder(_db, user_id))
    return await validate_selectable_chat_model(_db, model_id, user_id=user_id, scoped=scoped)


_READ_ONLY_TOOL_NOTE = (
    "A binding marked read-only (integration_read_only on create_agent / "
    "update_agent) keeps only the [read] tools; the [write] ones are "
    "withheld and refused."
)


async def _load_imported_tool_listing(
    user_id: str | None, provider: str = "", integration_id: uuid.UUID | None = None
) -> list[tuple[str, str, str, list[Any] | None]]:
    """``(id, name, provider, active_tools)`` of the integrations a caller may see.

    Every visible integration of *provider*, or the one *integration_id* names
    (any provider). ``active_tools`` is the imported spec's tools, ``None``
    when the integration has no spec. The listing half of
    ``list_integration_tools``: an integration whose API is its own
    imported spec keeps its tools there, never in the provider catalog.
    """
    from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
    from app.core.database import async_session as _builder_session
    from app.models.integration import Integration
    from app.models.integration_openapi_spec import IntegrationOpenAPISpec

    uid = safe_uuid(str(user_id)) if user_id else None
    async with _builder_session() as _db:
        group_ids = await get_user_group_ids(_db, uid) if uid else []
        stmt = (
            sa_select(
                Integration.id,
                Integration.name,
                Integration.provider,
                IntegrationOpenAPISpec.active_tools,
            )
            .outerjoin(
                IntegrationOpenAPISpec,
                IntegrationOpenAPISpec.integration_id == Integration.id,
            )
            .where(caller_visible_filter(Integration, uid, group_ids))
        )
        if integration_id is not None:
            stmt = stmt.where(Integration.id == integration_id)
        else:
            stmt = stmt.where(Integration.provider == provider)
        rows = (await _db.execute(stmt.order_by(Integration.name))).all()
    return [(str(row[0]), str(row[1]), str(row[2]), row[3]) for row in rows]


#: HTTP methods that only read: an imported operation using one whose tool
#: name still classifies as a write is flagged in the listing.
_READING_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "QUERY"})


def _format_imported_tool_listing(
    rows: list[tuple[str, str, str, list[Any] | None]],
) -> str | None:
    """The imported operations of spec-backed integrations, grouped by integration.

    Each tool's access is the one a read-only binding enforces (the shared
    name classifier); an HTTP method that reads where the name classifies as
    a write is flagged, since the binding withholds that tool. ``None`` when
    no integration has an imported operation.
    """
    from app.services.integration_read_only import integration_tool_access
    from app.services.openapi_import.service import imported_operations

    blocks: list[str] = []
    total = 0
    for iid, name, _provider, active_tools in rows:
        operations = imported_operations(active_tools)
        if not operations:
            blocks.append(f"**{name}** (`{iid}`): no imported operation (no spec attached).")
            continue
        access = [integration_tool_access(op["name"]) for op in operations]
        reads = access.count("read")
        total += len(operations)
        lines = [
            f"**{name}** (`{iid}`) - {len(operations)} tools "
            f"({reads} read, {len(operations) - reads} write):"
        ]
        for op, value in zip(operations, access, strict=True):
            request = f"{op['method']} {op['path']}".strip()
            line = f"  - {op['name']} [{value}] `{request}`"
            if op["operation"] != request:
                line += f" (operation `{op['operation']}`)"
            if value == "write" and op["method"] in _READING_HTTP_METHODS:
                line += " - a read by its method, withheld as a write by its name"
            lines.append(line)
        blocks.append("\n".join(lines))
    if total == 0:
        return None
    return "\n\n".join(blocks)


async def _load_writable_agent_for_builder(
    _db: AsyncSession,
    uid: uuid.UUID,
    agent_uuid: uuid.UUID,
    *,
    action: str = "modify",
    assignment_write: bool = False,
) -> tuple[Agent | None, str | None]:
    """Load an agent and verify the caller can mutate it.

    Mirrors the REST API's ``assert_writable_agent`` gates so the agent
    builder tool can never bypass UI-level restrictions (built-in
    read-only, company-managed admin-only, group-shared requires
    ``config``-tier). Returns ``(agent, None)`` on success or
    ``(None, error_message)`` on failure so callers can return the
    friendly message directly to the LLM.

    Applies the three-tier visibility filter (own + group-shared +
    company-managed, with the admin bypass of :func:`agent_tool_admin_bypass`)
    on the agent SELECT so a caller can never receive a permission-denied
    response — and therefore confirm the existence of — an agent they cannot
    see in the first place. This matches the REST API's ``get_user_agent``
    behaviour of returning 404 in both the not-found and not-visible cases.

    *assignment_write* (adding an assignment to the agent) looks three-tier
    first and takes the admin bypass only when that misses and
    :func:`agent_tool_admin_bypass` allows it (``AGENT_TOOLS_STRICT_ADMIN_ACCESS``
    off), so the one-time warning names a write that needed it.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.agent import Agent as _Agent

    user = await _load_caller_user_for_builder(_db, uid)
    if user is None:
        return None, "User not found."

    group_ids = await get_user_group_ids(_db, uid)

    async def _visible_agent(is_admin: bool):
        result = await _db.execute(
            sa_select(_Agent).where(
                _Agent.id == agent_uuid,
                user_visible_filter(_Agent, uid, group_ids, is_admin=is_admin),
            )
        )
        return result.scalar_one_or_none()

    bypassed = False
    if assignment_write:
        agent_row = await _visible_agent(False)
        if agent_row is None and agent_tool_admin_bypass(user):
            agent_row = await _visible_agent(True)
            bypassed = agent_row is not None
    else:
        agent_row = await _visible_agent(agent_tool_admin_bypass(user))
    if agent_row is None:
        # Existence-hiding by design: this covers both "row does not
        # exist" and "row exists but is invisible to the caller".
        # Surfacing a different message for the latter would leak the
        # existence of private agents owned by other users.
        return None, f"Agent `{agent_uuid}` not found."

    err = writable_agent_error(agent_row, user, group_ids, action=action)
    if err is not None:
        return None, err
    if bypassed:
        _report_admin_write_bypass("an assignment on agent", agent_uuid)
    return agent_row, None


async def _apply_agent_company_managed_toggle(
    _db: AsyncSession,
    agent,
    want_company_managed: bool,
    owner_fallback_id: uuid.UUID,
) -> str | None:
    """Flip an agent's company-managed (visibility) status, mirroring the REST
    ``PUT /agents/{id}`` toggle so the agent builder can never drift from it.

    Private -> company-managed: clears group sharing, strips personal
    (non-company-managed) bindings, mirrors the flag onto the agent's
    assignments + runs, and re-validates that every remaining binding is itself
    company-managed. Company-managed -> private: restores the owner (falling
    back to ``owner_fallback_id`` for legacy ownerless agents), clears the
    public-catalog flag, and reverts assignments + runs to private.

    The caller MUST enforce the admin gate first. Returns a friendly error
    string (the caller should roll back) or ``None`` on success.
    """
    from fastapi import HTTPException

    from app.models.assignment import Assignment, AssignmentRun

    # Lazy import to avoid a module-load cycle (agents.py imports builtin_tools
    # for the chat agent-builder tool). These are the single source of truth
    # for the toggle's side effects — reused, never reimplemented.
    from app.services.agent_bindings import (
        revert_company_managed_to_private,
        strip_personal_bindings,
        validate_company_managed_bindings,
    )
    from app.services.workset import retier_agent_worksets

    agent.is_company_managed = want_company_managed
    # The corpora the agent's chats and assignment runs built follow it, or
    # each later workset call of the agent is refused for the other tier.
    await retier_agent_worksets(_db, agent.id, want_company_managed)
    if want_company_managed:
        # Owner (agent.user_id) is preserved for provenance; only visibility
        # flips. Clear group sharing, strip personal bindings, then mirror the
        # flag onto assignments + runs (keeping their owner).
        agent.shared_group_ids = None
        agent.shared_group_permissions = None
        # This tool has no ``access_restricted`` knob, so the agent becomes a
        # plain company-managed one. Clearing the flag alongside the grants
        # preserves the "unrestricted company-managed ⇒ no grants" invariant
        # that ``apply_user_share_fields`` enforces on the REST path; leaving
        # it set while wiping the grants would leave a restricted agent with an
        # empty audience — visible to nobody but its owner and admins.
        agent.shared_user_ids = None
        agent.shared_user_permissions = None
        agent.access_restricted = False
        await strip_personal_bindings(_db, agent)
        await _db.execute(
            Assignment.__table__.update()
            .where(Assignment.agent_id == agent.id)
            .values(is_company_managed=True, user_id=agent.user_id)
        )
        await _db.execute(
            AssignmentRun.__table__.update()
            .where(AssignmentRun.agent_id == agent.id)
            .values(is_company_managed=True)
        )
        # Defence-in-depth: after stripping, every remaining binding should be
        # company-managed already, but re-validate to match the REST contract.
        try:
            await validate_company_managed_bindings(
                _db,
                mcp_server_ids=agent.mcp_server_ids,
                knowledge_base_ids=agent.knowledge_base_ids,
                integration_bindings=agent.integration_bindings,
                handover_agent_ids=agent.handover_agent_ids,
            )
        except HTTPException as exc:
            return str(exc.detail)
    else:
        # Company-managed -> private: restore visibility + ownership and clear
        # the run-as override (handled inside the revert helper).
        if agent.user_id is None:
            agent.user_id = owner_fallback_id
        agent.is_public_catalog = False
        await revert_company_managed_to_private(_db, agent.id, agent.user_id)
    return None


async def _load_writable_assignment_for_builder(
    _db: AsyncSession,
    uid: uuid.UUID,
    assignment_uuid: uuid.UUID,
    *,
    action: str = "modify",
):
    """Load an assignment + parent agent and verify the caller can mutate it.

    Mirrors the REST API's ``assert_writable_assignment`` gates so the
    same built-in / company-managed / group-permission constraints apply
    consistently across REST API and tool callers.

    Looks the assignment up through ``find_user_assignment``, the lookup the
    REST routes use (the parent agent's visibility), so a caller can never
    receive a permission-denied response — and therefore confirm the
    existence of — an assignment they cannot see. An admin whose three tiers
    miss it then gets the admin bypass the REST routes have, unless
    ``AGENT_TOOLS_STRICT_ADMIN_ACCESS`` (:func:`agent_tool_admin_bypass`).
    """
    from app.api.v1.helpers import find_user_assignment, get_user_group_ids
    from app.models.agent import Agent as _Agent

    user = await _load_caller_user_for_builder(_db, uid)
    if user is None:
        return None, None, "User not found."

    group_ids = await get_user_group_ids(_db, uid)

    # Without this guard the loader returns the permission-denied gate
    # response for assignments the caller cannot see — leaking their
    # existence (the same bug class as for agents).
    # Locked until the caller's commit: a write is validated against this row
    # (a schedule's cron against its stored trigger type), so a concurrent write
    # must not change it in between.
    assignment_row = await find_user_assignment(
        _db, uid, assignment_uuid, group_ids, lock=True
    )
    bypassed = False
    if assignment_row is None and agent_tool_admin_bypass(user):
        assignment_row = await find_user_assignment(
            _db, uid, assignment_uuid, group_ids, is_admin=True, lock=True
        )
        bypassed = assignment_row is not None
    if assignment_row is None:
        # Same existence-hiding contract as the agent loader.
        return None, None, f"Assignment `{assignment_uuid}` not found."

    agent_result = await _db.execute(
        sa_select(_Agent).where(_Agent.id == assignment_row.agent_id)
    )
    agent_row = agent_result.scalar_one_or_none()
    if agent_row is None:
        # The parent-agent row should always exist (FK constraint) —
        # treat a NULL as "not found" rather than leaking what we know
        # about the assignment we just resolved.
        return None, None, f"Assignment `{assignment_uuid}` not found."

    err = writable_assignment_error(
        assignment_row, agent_row, user, group_ids, action=action
    )
    if err is not None:
        return None, None, err
    if bypassed:
        _report_admin_write_bypass("assignment", assignment_uuid)
    return assignment_row, agent_row, None


#: How many run ids a refused ``delete_assignment`` names.
_DELETE_BLOCKING_RUNS_SHOWN = 5


async def _active_runs_blocking_delete(
    _db: AsyncSession, assignment_id: uuid.UUID
) -> list[uuid.UUID]:
    """The runs of the assignment still in flight, newest first.

    ``assignment_runs.assignment_id`` cascades on delete, so deleting an
    assignment with a run pending, delayed, running or awaiting a human
    deletes that run's record under it: the run ends "no longer recorded",
    its notes and summary lost. ``delete_assignment`` refuses while this is
    not empty. At most ``_DELETE_BLOCKING_RUNS_SHOWN + 1`` ids are read: the
    refusal names a few and says when there are more.
    """
    from app.models.assignment import AssignmentRun
    from app.services.assignment_engine import ACTIVE_RUN_STATUSES

    result = await _db.execute(
        sa_select(AssignmentRun.id)
        .where(
            AssignmentRun.assignment_id == assignment_id,
            AssignmentRun.status.in_(list(ACTIVE_RUN_STATUSES)),
        )
        .order_by(AssignmentRun.created_at.desc())
        .limit(_DELETE_BLOCKING_RUNS_SHOWN + 1)
    )
    return list(result.scalars().all())


def _delete_blocked_by_runs_message(
    assignment_id: str, name: str, run_ids: list[uuid.UUID]
) -> str:
    """The refusal of ``delete_assignment`` while runs are in flight."""
    shown = ", ".join(f"`{rid}`" for rid in run_ids[:_DELETE_BLOCKING_RUNS_SHOWN])
    more = " and more" if len(run_ids) > _DELETE_BLOCKING_RUNS_SHOWN else ""
    return (
        f"Assignment **{name}** (`{assignment_id}`) was not deleted: it has a run "
        f"in flight ({shown}{more}). Deleting it now would delete that run's "
        "record with it, including the run you may be calling from. Call "
        "`delete_assignment` again with `after_run=true`: it is paused now (no new "
        "run starts) and deleted by the platform once no run is active, which is "
        "how an assignment retires itself from its own run. Use `cancel_runs` for "
        "a stuck run."
    )


async def _schedule_assignment_retirement(
    _db: AsyncSession,
    uid: uuid.UUID,
    assignment,
    assignment_id: str,
    run_ids: list[uuid.UUID],
) -> str:
    """``delete_assignment(after_run=true)`` with runs in flight: pause it for deletion.

    The platform deletes it on the scheduler tick once none of its runs is
    active or queued (``services/assignment_retire.py``); its runs keep their
    records, before and after the deletion. Resuming the assignment cancels
    the deletion.
    """
    from app.models.user import User as _User
    from app.services.assignment_pause import actor_display_name, record_pause_audit
    from app.services.assignment_retire import RETIRE_AFTER_RUN_REASON, mark_retire_after_run

    acting_user = (
        await _db.execute(sa_select(_User).where(_User.id == uid))
    ).scalar_one_or_none()
    actor_label = actor_display_name(acting_user) if acting_user else str(uid)
    details = mark_retire_after_run(
        assignment, actor_user_id=uid, actor_label=actor_label, run_ids=run_ids
    )
    if acting_user is not None:
        await record_pause_audit(
            _db,
            assignment=assignment,
            user=acting_user,
            reason=RETIRE_AFTER_RUN_REASON,
            details=details,
        )
    name = assignment.name
    try:
        await _db.commit()
    except Exception:
        logger.exception(
            "delete_assignment(after_run) commit failed for assignment %s by user %s",
            assignment_id,
            uid,
        )
        await _db.rollback()
        return f"Failed to schedule the deletion of assignment `{assignment_id}`. Please try again."
    shown = ", ".join(f"`{rid}`" for rid in run_ids[:_DELETE_BLOCKING_RUNS_SHOWN])
    more = " and more" if len(run_ids) > _DELETE_BLOCKING_RUNS_SHOWN else ""
    return (
        f"Deletion of assignment **{name}** (`{assignment_id}`) scheduled at the end "
        f"of run(s) {shown}{more}: it is paused now (no new run starts) and the "
        "platform deletes it within a minute of its last run ending; the runs keep "
        "their records (get_run reads them after the deletion). Resume it to cancel "
        "the deletion."
    )


def _validate_id_arg(
    value: object, arg_name: str
) -> tuple[uuid.UUID | None, str | None]:
    """Validate a UUID-typed agent-builder tool argument.

    The LLM supplies ``agent_id`` / ``assignment_id`` as free-form strings, so
    a hallucinated or truncated value used to reach an inline
    ``uuid.UUID(value)`` and raise an uncaught ``ValueError`` — surfacing a raw
    tool crash to the model (or, in the ``create_assignment`` tool
    /``_add_assignment_to_agent``, a misleading "Failed to add assignment" with
    an error-level stack trace). Parsing once here lets every tool reject bad
    input *before* opening a DB session and hand the model an actionable
    correction instead.

    ``value`` is typed ``object`` because tool arguments come straight from
    JSON/LLM output and may not be a string at all. ``safe_uuid`` only guards
    ``ValueError``/``AttributeError``, so a non-string (e.g. ``bytes``) would
    still raise ``TypeError`` from ``uuid.UUID`` — we therefore treat any
    non-string as invalid up front rather than letting it crash.

    Returns ``(parsed_uuid, None)`` on success or ``(None, error_message)``.
    """
    parsed = safe_uuid(value) if isinstance(value, str) else None
    if parsed is None:
        # ``_bounded_display`` caps a hallucinated value (huge string / bytes /
        # container) so an invalid id can't force a multi-MB error string.
        return None, (
            f"{arg_name} must be a valid UUID (e.g. "
            f"'550e8400-e29b-41d4-a716-446655440000'). Got: {_bounded_display(value)}"
        )
    return parsed, None


#: Hard ceiling on any list tool's page size — keeps a single tool result
#: bounded even when the LLM asks for a huge page on a 600-agent platform.
_MAX_PAGE_SIZE = 200


def _coerce_page_args(
    limit: object, offset: object, *, default_limit: int
) -> tuple[int, int, str | None]:
    """Coerce LLM-supplied ``limit`` / ``offset`` pagination arguments.

    Tool arguments arrive as raw JSON, so the model may send an int, an
    integral float, a numeric string, or garbage. Blank / ``None`` fall back
    to the defaults;
    anything non-numeric returns an actionable error instead of a crash.
    ``limit`` is clamped to ``1..{_MAX_PAGE_SIZE}`` and ``offset`` to ``>= 0``
    so a hallucinated value can never turn a list tool into an unbounded dump.

    Returns ``(limit, offset, None)`` or ``(0, 0, error_message)``.
    """

    def _to_int(raw: object, fallback: int) -> int | None:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return fallback
        # bool is an int subclass — reject it explicitly (True would
        # silently become limit=1).
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw
        # JSON numbers may arrive as floats (e.g. ``20.0``) — accept them
        # when integral, reject fractional values.
        if isinstance(raw, float):
            return int(raw) if raw.is_integer() else None
        if isinstance(raw, str):
            text = raw.strip()
            try:
                return int(text)
            except ValueError:
                try:
                    numeric = float(text)
                except ValueError:
                    return None
                return int(numeric) if numeric.is_integer() else None
        return None

    parsed_limit = _to_int(limit, default_limit)
    if parsed_limit is None:
        return 0, 0, (
            f"limit must be an integer between 1 and {_MAX_PAGE_SIZE}. "
            f"Got: {_bounded_display(limit)}"
        )
    parsed_offset = _to_int(offset, 0)
    if parsed_offset is None:
        return 0, 0, (
            f"offset must be a non-negative integer. Got: "
            f"{_bounded_display(offset)}"
        )
    return (
        max(1, min(parsed_limit, _MAX_PAGE_SIZE)),
        max(0, parsed_offset),
        None,
    )


def _catalog_list_schema() -> dict[str, Any]:
    """The arguments ``list_skills`` and ``list_prompts`` share: a name filter,
    a one-line mode and paging. ``additionalProperties: false`` makes the loop
    refuse an unknown argument with the valid ones before the handler runs.
    A fresh dict per tool, so no definition shares a mutable schema."""
    return {
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "In names, else descriptions"},
            "compact": {"type": "boolean"},
            "limit": {"type": "integer"},
            "offset": {"type": "integer"},
        },
        "additionalProperties": False,
    }


def _catalog_list_page(
    rows: list[tuple[str, str, str, str]],
    *,
    noun: str,
    search: str = "",
    compact: bool = False,
    limit: object = None,
    offset: object = None,
) -> str:
    """One page of a skill or prompt listing.

    *rows* are ``(name, description, full_block, compact_line)`` in name
    order. ``search`` keeps the rows whose name holds it (any case), or,
    when no name does, those whose description does. A page cut by
    ``limit`` / ``offset`` opens with the shared paging line; an uncut page
    with no argument reads as the listing always did.
    """
    from .paging import page_line

    size, start, page_err = _coerce_page_args(limit, offset, default_limit=_MAX_PAGE_SIZE)
    if page_err:
        return page_err
    needle = (search or "").strip().casefold()
    matched_on = ""
    if needle:
        hits = [r for r in rows if needle in r[0].casefold()]
        matched_on = "name"
        if not hits:
            hits = [r for r in rows if needle in (r[1] or "").casefold()]
            matched_on = "description"
        rows = hits
    if not rows:
        return f"No {noun}s found" + (f" matching `{search.strip()}`." if needle else ".")
    total = len(rows)
    page = rows[start : start + size]
    head = f"Found {total} {noun}(s)" + (f" whose {matched_on} matches `{search.strip()}`" if needle else "") + ":\n"
    if len(page) < total:
        head = page_line(start, len(page), total, f"{noun}s") + "\n" + head
    body = [r[3] if compact else r[2] for r in page]
    return "\n".join([head, *body]) if not compact else head + "\n".join(body)


def _run_status_vocabulary() -> tuple[str, ...]:
    """Canonical run status vocabulary for the run tools.

    Single source of truth: the engine's ``ACTIVE_RUN_STATUSES`` +
    ``TERMINAL_RUN_STATUSES`` (includes the reserved "skipped" /
    "cancelled" states), imported lazily because ``assignment_engine``
    imports ``builtin_tools`` at module level. Shared by the ``list_runs``
    status validation AND its tool-schema description so the two can
    never drift.
    """
    from app.services.assignment_engine import (
        ACTIVE_RUN_STATUSES,
        TERMINAL_RUN_STATUSES,
    )

    return ACTIVE_RUN_STATUSES + TERMINAL_RUN_STATUSES


#: In-run deep search (``get_run`` with ``search``) — excerpt shaping caps.
#: Context chars are per side of the match; the per-section and total caps
#: keep the report bounded even when a needle appears hundreds of times in a
#: long conversation log.
_RUN_SEARCH_CONTEXT_CHARS = 160
_RUN_SEARCH_MAX_PER_SECTION = 5
_RUN_SEARCH_MAX_TOTAL = 25


def _search_section_excerpts(
    text: str,
    needle_lower: str,
    *,
    max_matches: int,
    context: int = _RUN_SEARCH_CONTEXT_CHARS,
) -> list[str]:
    """Return whitespace-collapsed excerpts around each match of *needle*.

    Case-insensitive; scanning restarts after each match so overlapping
    duplicates are not double-reported.
    """
    excerpts: list[str] = []
    lowered = text.lower()
    start = 0
    while len(excerpts) < max_matches:
        idx = lowered.find(needle_lower, start)
        if idx < 0:
            break
        begin = max(0, idx - context)
        end = min(len(text), idx + len(needle_lower) + context)
        body = " ".join(text[begin:end].split())
        prefix = "…" if begin > 0 else ""
        suffix = "…" if end < len(text) else ""
        excerpts.append(f"{prefix}{body}{suffix}")
        start = idx + max(len(needle_lower), 1)
    return excerpts


def _iter_run_search_sections(run, *, hide_parent: bool = False) -> list[tuple[str, str]]:
    """Return ``(label, text)`` pairs covering a run's full searchable content.

    This is the single place that defines what "search inside a run" means:
    result summary, error, HITL exchange, trigger payload/context, actions
    taken, and the full conversation log (``messages``) — the fields the
    cross-run ``list_runs`` search cannot afford to scan.
    *hide_parent*: the caller may not open the run's parent, so the payload
    keys naming it are not scanned (``without_hidden_parent``).
    """
    import json as _json

    from app.api.v1.helpers import without_hidden_parent

    def _dump(value) -> str:
        try:
            return _json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(value)

    sections: list[tuple[str, str]] = []
    for label in ("result_summary", "error", "human_input_question",
                  "human_input_response"):
        value = getattr(run, label, None)
        if value:
            sections.append((label, str(value)))
    # The durable trail of who answered which pause, gate decisions included:
    # the live question / response above are cleared at the resume.
    for i, entry in enumerate(getattr(run, "human_input_history", None) or []):
        sections.append((f"human_input_history[{i}]", _dump(entry)))
    for label in ("trigger_payload", "trigger_context", "actions_taken"):
        value = getattr(run, label, None)
        if not value:
            continue
        # Mirror the "safe to surface" contract of
        # ``_build_trigger_information`` (assignment_helpers): internal
        # webhook routing metadata (``_webhook_meta``) and the raw request
        # ``headers`` / ``body`` mirrors in ``trigger_context`` are never
        # scanned nor excerpted — the search must not return internal
        # metadata the prompt surfaces deliberately exclude.
        if label == "trigger_payload" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != "_webhook_meta"}
            value = without_hidden_parent(value, not hide_parent)
        elif label == "trigger_context" and isinstance(value, dict):
            value = {
                k: v
                for k, v in value.items()
                if k not in ("headers", "body")
            }
        if value:
            sections.append((label, _dump(value)))
    for i, msg in enumerate(getattr(run, "messages", None) or []):
        if isinstance(msg, dict):
            role = msg.get("role") or "?"
            name = msg.get("name")
            label = f"messages[{i}] ({role}{f'/{name}' if name else ''})"
            sections.append((label, _dump(msg)))
        else:
            sections.append((f"messages[{i}]", _dump(msg)))
    return sections


def _searchable_trigger_payload():
    """SQL expression for the cross-run searchable trigger-payload text.

    Subtracts ``_webhook_meta`` (internal webhook routing metadata) and the
    parent-run keys from the JSONB payload before casting to text, so the
    cross-run ``list_runs``
    search aligns with the in-run search's "safe to surface" contract
    (``_iter_run_search_sections`` strips the same key) — internal metadata
    can neither produce false-positive matches nor be probed via search.
    All payload writers coerce non-object bodies to ``{"raw_body": ...}``
    dicts, so the JSONB ``-`` operator only ever sees objects (or NULL,
    which propagates harmlessly through the ILIKE).

    The ``json -> jsonb`` parse resolves escape sequences, and jsonb cannot
    represent NUL nor a lone UTF-16 surrogate — one legacy row storing a
    ``\\u0000`` or ``\\ud800`` escape (an external webhook body) would fail
    the WHOLE query, not just that row. The engine's
    JSON serializer keeps both out of every new write (``core/database.py``),
    so the CASE guard exists for rows written before it: they yield NULL
    here — excluded from this one predicate but still matchable via
    agent/assignment name, result summary, error, and actions taken —
    instead of breaking the search for everyone. The guard deliberately
    never falls back to the raw payload text, which would leak
    ``_webhook_meta`` back into the search surface.

    The probe is exact (``core/json_sql.json_unparseable``): a payload that
    merely contains the literal six-character text ``\\u0000`` is stored as
    ``\\\\u0000`` — safe to parse — and is not excluded, nor is one holding a
    valid surrogate pair (an emoji).
    """
    from sqlalchemy import Text, cast
    from sqlalchemy.dialects.postgresql import JSONB, array

    from app.api.v1.helpers import PARENT_RUN_PAYLOAD_KEYS
    from app.core.json_sql import when_json_parseable
    from app.models.assignment import AssignmentRun

    # The keys naming a delegation's parent are not searched either: a
    # caller who may not open the parent must not probe a run by its id,
    # and ``get_run`` never shows them that match.
    unsearched = array(["_webhook_meta", *sorted(PARENT_RUN_PAYLOAD_KEYS)], type_=Text)
    return when_json_parseable(
        AssignmentRun.trigger_payload,
        cast(
            cast(AssignmentRun.trigger_payload, JSONB).op("-")(unsearched),
            Text,
        ),
        otherwise=None,
    )


def _run_degraded_lines(run: Any, *, prefix: str) -> str:
    """The run list's and ``get_run``'s lines for a run degraded by integration
    auth failures (#1589): the marker, then the failures. Empty for a clean run."""
    failures = getattr(run, "integration_auth_failures", None)
    if not failures:
        return ""
    marker = (
        "Degraded: needs attention, no integration was readable (result not assessable)"
        if getattr(run, "stop_reason", None) == UNREADABLE_STOP_REASON
        else "Degraded: partial integration coverage"
    )
    return f"{prefix}{marker}{prefix}{auth_failures_line(failures)}"


def _event_latency_line(run) -> str | None:
    """``get_run``'s line for an event-started run: the event's age when it started.

    Timestamps only (``tracing.event_latency``): the event (``occurred_at``,
    else the receipt) to the run's start, and the part of it spent queued.
    ``None`` for a run no event started, or not started yet.
    """
    from app.services.tracing.event_latency import event_latency

    try:
        figures = event_latency(run.trigger_context, created_at=run.created_at, started_at=run.started_at)
    except Exception:
        return None
    if not figures or figures.get("event_to_run_ms") is None:
        return None
    line = f"Event to start: {figures['event_to_run_ms'] / 1000:.1f}s ({figures['kind']}"
    if figures.get("queue_ms") is not None:
        line += f", {figures['queue_ms'] / 1000:.1f}s queued"
    return line + ")"


#: Entries of ``human_input_history`` get_run lists (the newest ones).
_HUMAN_INPUT_HISTORY_SHOWN = 20


def _human_input_history_lines(run: Any) -> list[str]:
    """get_run's "Human input history" block: who answered each pause, when,
    from where, and each gated call's outcome (``human_input_history``).

    A pause the deadline ended reads ``by Deadline``: never a person.
    """
    history = [e for e in (getattr(run, "human_input_history", None) or []) if isinstance(e, dict)]
    if not history:
        return []
    shown = history[-_HUMAN_INPUT_HISTORY_SHOWN:]
    lines = ["", f"Human input history ({len(history)}):"]
    if len(shown) < len(history):
        lines.append(f"(the {len(shown)} newest; search=... reads every entry)")
    for entry in shown:
        who = entry.get("responded_by") or "unknown"
        if entry.get("responded_by_id"):
            who += f" (id: `{entry['responded_by_id']}`)"
        head = f"- {entry.get('responded_at') or '?'}: {entry.get('resolution') or '?'} by {who}"
        if entry.get("surface"):
            head += f" via {entry['surface']}"
        lines.append(head)
        if entry.get("response"):
            lines.append(f"  answer: {entry['response']}")
        for gate in entry.get("gate_outcomes") or []:
            if isinstance(gate, dict):
                name = gate.get("tool_name") or "?"
                lines.append(f"  {name} ({gate.get('tool_call_id') or '?'}): {gate.get('outcome')}")
    return lines


def _search_run_content(run, search: str, *, hide_parent: bool = False) -> str:
    """Grep a single run's full content and format a bounded excerpt report."""
    from app.services.assignment_helpers import _UNTRUSTED_TRIGGER_TYPES
    from app.services.content_safety import wrap_untrusted

    # Trigger payloads/contexts of webhook / integration-event runs carry
    # attacker-influenceable external data — quarantine their excerpts in an
    # untrusted-content block, mirroring ``_build_trigger_information``.
    trigger_type = None
    if isinstance(getattr(run, "trigger_context", None), dict):
        trigger_type = run.trigger_context.get("type")
    untrusted_trigger = trigger_type in _UNTRUSTED_TRIGGER_TYPES

    needle_lower = search.lower()
    total = 0
    parts: list[str] = []
    for label, text in _iter_run_search_sections(run, hide_parent=hide_parent):
        if total >= _RUN_SEARCH_MAX_TOTAL:
            parts.append(
                f"\n(Stopped after {_RUN_SEARCH_MAX_TOTAL} matches — "
                "narrow the search term for more precision.)"
            )
            break
        excerpts = _search_section_excerpts(
            text,
            needle_lower,
            max_matches=min(
                _RUN_SEARCH_MAX_PER_SECTION,
                _RUN_SEARCH_MAX_TOTAL - total,
            ),
        )
        if not excerpts:
            continue
        total += len(excerpts)
        parts.append(f"\n## {label}")
        section_body = "\n".join(f"• {e}" for e in excerpts)
        if untrusted_trigger and label in (
            "trigger_payload",
            "trigger_context",
        ):
            section_body = wrap_untrusted(
                section_body, source=f"{trigger_type} trigger ({label})"
            )
        parts.append(section_body)
    if not parts:
        return (
            f"No matches for `{search}` in this run (searched result "
            "summary, error, human-input exchange and history, trigger payload, "
            "trigger context, actions taken, and the full conversation "
            "log). The run likely never touched what you are looking for "
            "— try other candidate runs, or conclude that no run handled "
            "this item."
        )
    header = f"Found {total} match(es) for `{search}` in run `{run.id}`:"
    return "\n".join([header, *parts])


#: The value (case-insensitive) the tool schema declares to clear a tri-state
#: override back to "inherit from the parent agent": one declared value, never
#: a list of synonyms. Single source of truth for the parser, its error message
#: and the tool-schema docs.
INHERIT_VALUE = "inherit"


def _parse_optional_bool_arg(
    raw: object, *, allow_inherit: bool = False
) -> tuple[bool | None, str | None, bool]:
    """Parse a string-typed boolean argument for the ``update_assignment`` tool.

    The update tools express "leave unchanged" as a blank string (the same
    convention used by ``enabled`` / ``delay``), so a blank string or ``None``
    returns ``provided=False`` and the caller skips the field. A real JSON
    ``bool`` (tool kwargs are forwarded verbatim, so a model may emit one) and
    the strings ``true`` / ``false`` (case-insensitive) map to the boolean. Any
    other value - a non-true/false string, or a non-bool / non-string / non-None
    type such as an int - returns an actionable error (``provided=True``) instead
    of being silently skipped. When *allow_inherit* is set, the
    :data:`INHERIT_VALUE` (``inherit``) resolves to ``None`` (clear the
    override so the value inherits from the parent agent — used for the
    tri-state ``human_in_the_loop`` field).

    Returns ``(value, error, provided)``. On the error path ``provided`` is
    ``True`` and ``error`` is a friendly, actionable message the caller MUST
    return without mutating the assignment.
    """
    # Accept a real JSON bool too: tool-call kwargs are forwarded verbatim (no
    # schema coercion), so an LLM that emits true/false for these string-typed
    # fields must still take effect rather than silently no-op.
    if isinstance(raw, bool):
        return raw, None, True
    # ``None`` (omitted / JSON null) means "leave unchanged".
    if raw is None:
        return None, None, False
    if isinstance(raw, str):
        if not raw.strip():
            return None, None, False
        normalized = raw.strip().lower()
        if normalized in ("true", "false"):
            return normalized == "true", None, True
        if allow_inherit and normalized == INHERIT_VALUE:
            return None, None, True
    # A non-true/false string, or any other non-bool / non-string / non-None
    # type (e.g. an int), is an explicit invalid value: report an actionable
    # error rather than silently skipping the field (mirrors
    # ``_parse_optional_int_arg``).
    if allow_inherit:
        allowed = f"'true', 'false', or '{INHERIT_VALUE}' to inherit from the agent"
    else:
        allowed = "'true' or 'false'"
    return None, f"must be {allowed}", True


def _parse_optional_int_arg(
    raw: object,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> tuple[int | None, str | None, bool]:
    """Parse a string-typed integer argument for the ``update_assignment`` tool.

    A blank string, ``None``, or omitted arg means "leave unchanged"
    (``provided=False``). A real ``int`` or a numeric string is accepted (tool
    kwargs are forwarded verbatim, so a caller may pass ``{"max_iterations": 20}``
    as a JSON number); a ``bool``, a float, or any other type, a non-numeric
    string, or a value outside the inclusive ``[minimum, maximum]`` range returns
    a friendly error so the LLM can correct it instead of persisting a nonsensical
    limit (e.g. a negative ``timeout_seconds``).

    Returns ``(value, error, provided)``.
    """
    # ``bool`` is a subclass of ``int`` but is never a valid limit, so reject it
    # before the ``int`` branch.
    if isinstance(raw, bool):
        return None, f"{label} must be an integer.", True
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            value = int(raw.strip())
        except ValueError:
            return None, f"{label} must be an integer.", True
    elif raw is None or isinstance(raw, str):
        # ``None`` or a blank string = leave unchanged.
        return None, None, False
    else:
        # Any other non-string type (e.g. a float) is not a valid integer limit.
        return None, f"{label} must be an integer.", True
    if minimum is not None and value < minimum:
        return None, f"{label} must be at least {minimum}.", True
    if maximum is not None and value > maximum:
        return None, f"{label} must be at most {maximum}.", True
    return value, None, True


#: The trigger types the assignment tools accept: the REST API's and the UI's
#: set. ``work_item`` makes the assignment its agent's inbox: the scheduler tick
#: claims the oldest open item the agent owns and runs it.
ASSIGNMENT_TRIGGER_TYPES: tuple[str, ...] = (
    "schedule",
    "webhook",
    "integration_event",
    "manual",
    "work_item",
    "platform_event",
)


def platform_event_trigger_error(trigger_type: object, trigger_config: object) -> str | None:
    """Return the refusal for a ``platform_event`` trigger as it will be saved, else ``None``.

    The trigger's ``filter`` expression too, for every filtered trigger type.
    """
    from app.services.integration_event_filters import trigger_filter_error
    from app.services.platform_events import trigger_config_error

    return trigger_config_error(trigger_type, trigger_config) or trigger_filter_error(trigger_type, trigger_config)


def work_item_trigger_error(trigger_type: object, trigger_config: object) -> str | None:
    """Return the refusal for a ``work_item`` trigger AS IT WILL BE SAVED, else ``None``.

    The options are the ones the trigger editor stores and the dispatch reads
    (``services/work_item.assignment_inbox``): ``correlation_prefix`` (a string),
    ``priority_min`` (a work-item priority), ``owner_agent_id`` (the agent
    whose queue a dedicated dispatcher drains, a UUID) and
    ``max_concurrent_runs`` (how many items run in parallel, 1 to 10) and
    ``express_lane`` (extra slots for some correlation prefixes).
    ``{}`` drains the assignment's own agent's queue one item at a time. Any
    other trigger type is not judged here.
    """
    from app.models.work_item import WORK_ITEM_PRIORITIES
    from app.services.assignment_dispatch import work_item_concurrency_error

    if trigger_type != "work_item":
        return None
    if not isinstance(trigger_config, dict):
        return "Invalid trigger_config: a work_item trigger takes a JSON object."
    prefix = trigger_config.get("correlation_prefix")
    if prefix is not None and not isinstance(prefix, str):
        return "Invalid trigger_config: correlation_prefix must be a string."
    priority = trigger_config.get("priority_min")
    if priority not in (None, "") and priority not in WORK_ITEM_PRIORITIES:
        return (
            "Invalid trigger_config: priority_min must be one of "
            f"{', '.join(WORK_ITEM_PRIORITIES)}."
        )
    owner = trigger_config.get("owner_agent_id")
    if owner not in (None, "") and safe_uuid(owner) is None:
        return "Invalid trigger_config: owner_agent_id must be an agent id (a UUID)."
    return work_item_concurrency_error(trigger_config)


def _parse_trigger_config_arg(raw: str) -> tuple[dict | None, str | None]:
    """Decode the JSON-string ``trigger_config`` argument of the assignment tools.

    Valid JSON is not enough: the column is a NOT NULL object. ``"null"`` decodes
    to ``None``, which SQLAlchemy writes as the JSON literal ``null`` - the
    commit succeeds and every later read of the assignment fails to serialize -
    and a list, string or number breaks every ``trigger_config.get``
    downstream.

    Returns ``(config, error)``; exactly one of the two is ``None``.
    """
    example = '(e.g. \'{"cron": "0 9 * * 1-5"}\')'
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, f"Invalid trigger_config: must be a valid JSON string {example}"
    if not isinstance(parsed, dict):
        return None, (
            f"Invalid trigger_config: must decode to a JSON object {example}, "
            f"got {type(parsed).__name__}."
        )
    # A ``"\ud800"`` escape decodes to a lone surrogate, stored as a row every
    # read fails on.
    surrogate_err = lone_surrogate_error("trigger_config", parsed)
    if surrogate_err is not None:
        return None, f"Invalid trigger_config: {surrogate_err}"
    return parsed, None


def _parse_kb_permissions_arg(
    raw: object,
) -> tuple[dict[str, str] | None, str | None, bool]:
    """Parse the agent-builder ``knowledge_base_permissions`` argument.

    Mirrors the per-KB write-access contract of the REST ``PUT /agents/{id}``
    endpoint and the frontend Knowledge config card. Format: comma-separated
    ``<knowledge_base_id>:<read|write>`` pairs. Read is the default, so only a
    ``write`` grant is meaningful — a ``read`` entry is treated the same as
    omitting the KB (matching the frontend, which drops read entries before
    persisting). The sentinels ``none`` / ``clear`` reset every grant so all
    bound KBs become read-only.

    Returns ``(permissions, error, provided)``:

    * ``provided`` is ``False`` when *raw* is blank / non-string ("leave
      unchanged"); the caller must skip the field entirely.
    * On a parse error ``error`` is a friendly, actionable message and the
      caller MUST return it without mutating the agent.
    * Otherwise ``permissions`` is the ``{kb_id: "write"}`` map (possibly empty,
      which the caller stores as ``None`` = all bound KBs read-only). Stale
      grants for unbound KBs are NOT pruned here — the caller prunes against the
      agent's resolved ``knowledge_base_ids`` via
      ``prune_orphaned_knowledge_base_permissions`` so a grant set in the same
      call as the binding is honoured.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, None, False
    cleaned_raw = raw.strip()
    if cleaned_raw.lower() in ("none", "clear"):
        return {}, None, True
    perms: dict[str, str] = {}
    malformed: list[str] = []
    valid_pairs = 0
    for raw_pair in cleaned_raw.split(","):
        cleaned = raw_pair.strip()
        if not cleaned:
            continue
        kid, sep, perm = cleaned.partition(":")
        kid, perm = kid.strip(), perm.strip().lower()
        if not sep or not kid or perm not in ("read", "write"):
            malformed.append(cleaned)
            continue
        try:
            kid = str(uuid.UUID(kid))
        except ValueError:
            malformed.append(cleaned)
            continue
        valid_pairs += 1
        # Read is the default — only retain explicit write grants so the stored
        # map matches what the REST API / frontend persist.
        if perm == "write":
            perms[kid] = "write"
    if malformed:
        return (
            None,
            (
                "Invalid knowledge_base_permissions entry(ies): "
                f"{', '.join(malformed)}. Use 'KB_ID:write' or "
                "'KB_ID:read' pairs (KB_ID must be a "
                "valid UUID) separated by commas (e.g. 'UUID:write,UUID:read'), "
                "or 'none'/'clear' to reset all knowledge bases to read-only."
            ),
            True,
        )
    if valid_pairs == 0:
        # Non-blank, not the ``none``/``clear`` sentinel, yet no parseable pair
        # survived (e.g. ``","`` or ``", ,"`` — only separators / blanks).
        # Refuse instead of returning an empty map: the caller reads an empty
        # map as "clear every write grant", but only the explicit ``none`` /
        # ``clear`` sentinel may do that, so a stray separator in tool output
        # must not silently wipe write access.
        return (
            None,
            (
                "knowledge_base_permissions contained no valid "
                "'KB_ID:write' / 'KB_ID:read' pairs. "
                "Provide at least one pair, use 'none' or 'clear' to reset all "
                "knowledge bases to read-only, or omit the argument to leave the "
                "current grants unchanged."
            ),
            True,
        )
    return perms, None, True


async def _trigger_integration_error(
    _db: AsyncSession,
    uid: uuid.UUID,
    trigger_config: object,
    *,
    baseline: object = None,
) -> str | None:
    """The answer of the assignment tools when a trigger names a new
    integration the caller cannot see (the REST rule, with the admin
    bypass of :func:`agent_tool_admin_bypass`), else ``None``.

    Nothing is read when the trigger names no integration or the one already
    stored, whatever its spelling.
    """
    from types import SimpleNamespace

    from app.api.v1.binding_access import (
        TRIGGER_INTEGRATION_REFUSED_DETAIL,
        trigger_integration_ref,
        trigger_integration_refused,
        trigger_integration_unchanged,
    )

    if trigger_integration_ref(trigger_config) is None:
        return None
    if trigger_integration_unchanged(trigger_config, baseline):
        return None
    caller = await _load_caller_user_for_builder(_db, uid)
    if await trigger_integration_refused(
        _db,
        user=caller or SimpleNamespace(id=uid, is_admin=False),
        value=trigger_config,
        baseline=baseline,
        is_admin=agent_tool_admin_bypass(caller),
    ):
        return TRIGGER_INTEGRATION_REFUSED_DETAIL
    return None


# ── Generic scalar/list/JSON argument parsers (shared by update_agent) ─────────
# These mirror the verbatim-kwargs contract of ``_parse_optional_int_arg``: tool
# arguments are forwarded as-is, so a caller may pass a native bool/number/dict
# (JSON) or a string. A blank string / ``None`` always means "leave unchanged"
# so omitting a field can never overwrite it.


def _parse_bool_arg(raw: object, label: str) -> tuple[bool | None, str | None, bool]:
    """Parse a tri-state boolean argument.

    Accepts a native ``bool`` or the strings ``"true"`` / ``"false"`` (trimmed,
    case-insensitive). Blank / ``None`` = leave unchanged (``provided=False``).
    Anything else is a friendly error. Returns ``(value, error, provided)``.
    """
    if isinstance(raw, bool):
        return raw, None, True
    if raw is None:
        return None, None, False
    if isinstance(raw, str):
        s = raw.strip().lower()
        if not s:
            return None, None, False
        if s == "true":
            return True, None, True
        if s == "false":
            return False, None, True
    return None, f"{label} must be 'true' or 'false'.", True


def _parse_optional_float_arg(
    raw: object,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    clear_sentinels: tuple[str, ...] = ("none", "default", "inherit"),
) -> tuple[float | None, str | None, bool]:
    """Parse an optional float that also accepts a clear sentinel.

    Returns ``(value, error, provided)``. ``provided=False`` = leave unchanged
    (blank / ``None``). When provided, a ``value`` of ``None`` means "clear to
    the platform default" (the ``clear_sentinels`` were supplied); otherwise
    ``value`` is the parsed float, validated to lie within
    ``[minimum, maximum]`` — an out-of-range value returns a friendly error
    (it is NOT clamped, mirroring the REST endpoint, which rejects too).
    """
    if isinstance(raw, bool):
        return None, f"{label} must be a number.", True
    if isinstance(raw, int | float):
        value = float(raw)
    elif raw is None:
        return None, None, False
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None, None, False
        if s.lower() in clear_sentinels:
            return None, None, True  # clear → platform default
        try:
            value = float(s)
        except ValueError:
            return None, f"{label} must be a number.", True
    else:
        return None, f"{label} must be a number.", True
    if minimum is not None and value < minimum:
        return None, f"{label} must be at least {minimum}.", True
    if maximum is not None and value > maximum:
        return None, f"{label} must be at most {maximum}.", True
    return value, None, True


def _csv_str_list(raw: str) -> list[str]:
    """Split a comma-separated string into trimmed, non-empty tokens."""
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _parse_integration_pairs(raw: str) -> tuple[dict[str, list[str]], list[str]]:
    """``{provider: [canonical ids]}`` from ``'provider:id,provider:id'``, and the
    malformed entries (no ``:``, or an id that is not a UUID)."""
    pairs: dict[str, list[str]] = {}
    malformed: list[str] = []
    for token in _csv_str_list(raw):
        provider, sep, iid = token.partition(":")
        provider, parsed = provider.strip().lower(), safe_uuid(iid.strip())
        if not sep or not provider or parsed is None:
            malformed.append(token)
            continue
        ids = pairs.setdefault(provider, [])
        if str(parsed) not in ids:
            ids.append(str(parsed))
    return pairs, malformed


def _parse_uuid_csv(raw: str) -> tuple[list[str], list[str]]:
    """Canonical UUIDs from a comma-separated string, and the invalid tokens."""
    ids: list[str] = []
    invalid: list[str] = []
    for token in _csv_str_list(raw):
        parsed = safe_uuid(token)
        if parsed is None:
            invalid.append(token)
        elif str(parsed) not in ids:
            ids.append(str(parsed))
    return ids, invalid


#: What a group may do with a shared integration as the tools name it,
#: and the share tier it is stored as (``helpers.SHARE_PERMISSION_*``): "use"
#: binds and runs it, "manage" also edits it. A group with no entry is "use".
_INTEGRATION_SHARE_TIERS = {
    "use": "read_only",
    "read_only": "read_only",
    "manage": "config",
    "config": "config",
}


def _share_tier_label(tier: str | None) -> str:
    return "manage" if tier == "config" else "use"


#: The access a person is given to an agent as the tools name it, and the tier
#: it is stored as (``helpers.SHARE_PERMISSIONS_ALL``): ``read_only`` sees and
#: chats with the agent, ``config`` also edits it, ``config_with_chats`` also
#: reads its chats. The dialog's own words are read too.
_AGENT_SHARE_TIERS = {
    "read_only": "read_only",
    "readonly": "read_only",
    "use": "read_only",
    "view": "read_only",
    "chat": "read_only",
    "config": "config",
    "configure": "config",
    "configuration": "config",
    "edit": "config",
    "manage": "config",
    "config_with_chats": "config_with_chats",
    "configuration_with_chats": "config_with_chats",
    "config_and_chats": "config_with_chats",
}

#: Members a ``list_groups(members=true)`` call names per group before it counts the rest.
_MAX_GROUP_MEMBERS_LISTED = 50


def _agent_share_tier(raw: object) -> str | None:
    """One of ``helpers.SHARE_PERMISSIONS_ALL`` for a ``users_permission`` value, or ``None``."""
    key = re.sub(r"[\s-]+", "_", str(raw).strip().lower())
    return _AGENT_SHARE_TIERS.get(key)


async def _group_member_counts(_db: AsyncSession, group_ids: list[str]) -> dict[str, int]:
    """``{group id: member count}`` for *group_ids*, the count ``GET /groups`` serves everyone."""
    from sqlalchemy import func

    from app.models.group import GroupMember

    valid = [u for g in group_ids if (u := safe_uuid(g))]
    if not valid:
        return {}
    rows = await _db.execute(
        sa_select(GroupMember.group_id, func.count(GroupMember.id))
        .where(GroupMember.group_id.in_(valid))
        .group_by(GroupMember.group_id)
    )
    return {str(group_id): int(count or 0) for group_id, count in rows.all()}


async def _agent_audience_lines(_db: AsyncSession, agent: Any) -> list[str]:
    """The ``Shared with groups`` / ``Shared with people`` lines of an agent.

    Groups by name with their member count (what ``GET /groups`` serves every
    user), people by name and email (what ``GET /users/directory?ids=`` serves
    whoever holds the ids - the agent's readers do), each with its tier and
    its id so a read-modify-write of ``shared_group_ids`` / ``users`` is exact.
    """
    from app.services.share_principals import entity_grants, load_principals, stored_grants

    index = await load_principals(_db, agent)
    groups = stored_grants(*entity_grants(agent, "group"))
    people = stored_grants(*entity_grants(agent, "user"))
    counts = await _group_member_counts(_db, [key for key, _tier in groups])

    def _group_line() -> str:
        if not groups:
            return "None"
        items = []
        for key, tier in groups:
            count = counts.get(key)
            members = "" if count is None else f", {count} member{'' if count == 1 else 's'}"
            items.append(f"{index.group(key).label} (`{key}`{members}) [{tier}]")
        return f"{len(groups)} — " + ", ".join(items)

    def _people_line() -> str:
        if not people:
            return "None"
        items = []
        for key, tier in people:
            principal = index.user(key)
            state = "" if principal.is_active else ", deactivated"
            items.append(f"{principal.label} (`{key}`{state}) [{tier}]")
        return f"{len(people)} — " + ", ".join(items)

    lines = [
        f"  • Shared with groups: {_group_line()}",
        f"  • Shared with people: {_people_line()}",
    ]
    if getattr(agent, "is_company_managed", False):
        restricted = "Yes" if getattr(agent, "access_restricted", False) else "No"
        lines.append(f"  • Access restricted to the groups and people above: {restricted}")
    return lines


def _parse_group_sharing_args(
    group_ids: object, permissions: object
) -> tuple[list[str] | None, dict[str, str] | None, str | None, bool]:
    """``(ids, permissions, error, provided)`` for the group-sharing arguments.

    ``ids`` / ``permissions`` are ``None`` when that argument was not given
    (left unchanged); ``none`` / ``clear`` give an empty value. Group ids are a
    comma-separated string or a JSON array; permissions a JSON object
    ``{group_id: "use" | "manage"}`` or ``group_id:use`` pairs.
    """
    ids_given, perms_given = _arg_provided(group_ids), _arg_provided(permissions)
    if not ids_given and not perms_given:
        return None, None, None, False

    ids: list[str] | None = None
    if ids_given:
        if isinstance(group_ids, list):
            tokens = [str(g) for g in group_ids]
        elif isinstance(group_ids, str):
            text = group_ids.strip()
            if text.lower() in ("none", "clear"):
                tokens = []
            elif text.startswith("["):
                try:
                    tokens = [str(g) for g in json.loads(text)]
                except (ValueError, TypeError):
                    return None, None, "shared_group_ids must be group UUIDs (comma-separated or a JSON array).", True
            else:
                tokens = _csv_str_list(text)
        else:
            return None, None, "shared_group_ids must be group UUIDs (comma-separated or a JSON array).", True
        ids, invalid = _parse_uuid_csv(",".join(tokens))
        if invalid:
            return None, None, f"Invalid shared_group_ids: {', '.join(invalid)} - each must be a group UUID.", True

    perms: dict[str, str] | None = None
    if perms_given:
        raw: object = permissions
        if isinstance(raw, str):
            text = raw.strip()
            if text.lower() in ("none", "clear"):
                raw = {}
            elif text.startswith("{"):
                try:
                    raw = json.loads(text)
                except ValueError:
                    raw = None
            else:
                tokens = _csv_str_list(text)
                raw = (
                    dict(token.rsplit(":", 1) for token in tokens)
                    if tokens and all(":" in token for token in tokens)
                    else None
                )
        if not isinstance(raw, dict):
            return None, None, (
                "shared_group_permissions must be a JSON object {group_id: 'use' | 'manage'} "
                "or 'group_id:use' pairs."
            ), True
        perms = {}
        bad: list[str] = []
        for group_id, tier in raw.items():
            parsed = safe_uuid(str(group_id).strip())
            stored = _INTEGRATION_SHARE_TIERS.get(str(tier).strip().lower())
            if parsed is None or stored is None:
                bad.append(f"{group_id}:{tier}")
                continue
            perms[str(parsed)] = stored
        if bad:
            return None, None, (
                f"Invalid shared_group_permissions entry(ies): {', '.join(bad)}. Each key "
                "is a group UUID and each value 'use' (bind and run it) or 'manage' (also edit it)."
            ), True
    return ids, perms, None, True


async def _apply_group_sharing(
    _db: AsyncSession,
    entity: Any,
    ids: list[str] | None,
    perms: dict[str, str] | None,
) -> str | None:
    """Write parsed group-sharing arguments onto *entity*; the refusal, or ``None``.

    Through ``normalize_group_grants``, the REST routes' writer: an
    added group must exist, a stored one whose group was deleted is dropped,
    and the groups are locked against a concurrent deletion.
    """
    from fastapi import HTTPException

    from app.api.v1.helpers import normalize_group_grants

    final_ids = ids if ids is not None else [str(g) for g in entity.shared_group_ids or []]
    current = dict(entity.shared_group_permissions or {})
    if perms is not None:
        stray = [g for g in perms if g not in final_ids]
        if stray:
            return (
                "shared_group_permissions names groups the integration is not shared "
                f"with: {', '.join(stray)}. Add them to shared_group_ids."
            )
        current = perms
    try:
        entity.shared_group_ids, entity.shared_group_permissions = (
            await normalize_group_grants(
                _db, final_ids, current, existing_ids=entity.shared_group_ids
            )
        )
    except HTTPException as exc:
        return f"{exc.detail}"
    return None


#: What a person or a group may do with a shared skill or prompt as the tools
#: name it, and the tier it is stored as: "use" attaches and reads it,
#: "manage" also edits it (the integration words, the same stored tiers).
_CATALOG_SHARE_TIERS = _INTEGRATION_SHARE_TIERS

#: The owner and sharing arguments ``update_skill`` and ``update_prompt`` share.
_CATALOG_ACCESS_PROPERTIES: dict[str, Any] = {
    "owner": {"type": "string", "description": "Email or user id"},
    "users": {
        "type": ["array", "string"],
        "items": {"type": "string"},
        "description": "Emails or user ids; 'none' clears",
    },
    "users_permission": {"type": "string", "enum": ["use", "manage"]},
    "shared_group_ids": {"type": "string", "description": "Group UUIDs; 'none' clears"},
    "shared_group_permissions": {"type": "string", "description": "JSON {group_id: use|manage}"},
    "binding_mode": {"type": "string", "enum": ["replace", "add", "remove"]},
}

_CATALOG_ACCESS_DESCRIPTION = (
    " Owner change: administrator only; sharing as update_agent."
)


async def _catalog_access_lines(_db: AsyncSession, entity: Any) -> list[str]:
    """The owner and audience lines of a skill or a prompt (``get_skill`` / ``get_prompt``).

    The owner by name and email, then the groups and the people it is shared
    with, each with its tier and id (the agent's reading, ``_agent_audience_lines``).
    """
    from app.models.user import User
    from app.services.share_principals import person_label

    owner = "None (company-managed)" if getattr(entity, "is_company_managed", False) else "None"
    owner_id = getattr(entity, "user_id", None)
    if owner_id is not None:
        rows = await _db.execute(
            sa_select(User.email, User.full_name).where(User.id == owner_id)
        )
        found = rows.all()
        owner = (
            f"{person_label(found[0][1], found[0][0])} (`{owner_id}`)"
            if found
            else f"a deleted account (`{owner_id}`)"
        )
    return [f"  • Owner: {owner}", *await _agent_audience_lines(_db, entity)]


async def _apply_catalog_access(
    _db: AsyncSession,
    entity: Any,
    *,
    noun: str,
    entity_type: str,
    caller: Any,
    owner: object,
    users: object,
    users_permission: object,
    shared_group_ids: object,
    shared_group_permissions: object,
    binding_mode: object,
) -> tuple[list[str], list[str], str | None]:
    """Owner transfer and sharing of a skill or a prompt: ``(updated, lines, refusal)``.

    The writes are the REST routes' own: ``apply_owner_transfer`` (admin only,
    the ``transfer_ownership`` audit entry), ``apply_user_share_fields`` and
    ``normalize_group_grants`` (through ``_apply_group_sharing``). ``binding_mode``
    applies the people and the groups as on ``update_agent``: 'replace' (the
    default), 'add' or 'remove'. A refusal changes nothing: the caller rolls back.
    """
    from fastapi import HTTPException

    from app.api.v1.helpers import apply_owner_transfer, apply_user_share_fields
    from app.services.share_principals import (
        MAX_PRINCIPALS_PER_CALL,
        PrincipalIndex,
        SharingChange,
        plan_grants,
        principal_entries,
        resolve_principals,
        stored_grants,
    )

    updated: list[str] = []
    lines: list[str] = []
    owner_given = _arg_provided(owner)
    users_given = _arg_provided(users)
    tier_given = _arg_provided(users_permission)
    group_ids, group_perms, group_err, groups_given = _parse_group_sharing_args(
        shared_group_ids, shared_group_permissions
    )
    if not (owner_given or users_given or tier_given or groups_given):
        return updated, lines, None
    if group_err:
        return updated, lines, group_err
    op = (binding_mode or "").strip().lower() if isinstance(binding_mode, str) else ""
    op = op or "replace"
    if op not in ("replace", "add", "remove"):
        return updated, lines, (
            f"Invalid binding_mode '{binding_mode}': use 'replace' (default), 'add' or 'remove'."
        )

    # 1. The owner, first: the people named next are judged against the new one.
    if owner_given:
        if not isinstance(owner, str):
            return updated, lines, "owner is one email address or user id."
        # The admin gate comes before the lookup, as in ``apply_owner_transfer``:
        # a non-admin never learns whether an address has an account.
        if not getattr(caller, "is_admin", False):
            return updated, lines, (
                f"Only an administrator can change the owner of {noun} **{entity.name}**. "
                "Nothing was changed."
            )
        resolved = await resolve_principals(_db, [owner.strip()])
        refusal = resolved.refusal(granting=True)
        if refusal or not resolved.users:
            return updated, lines, f"Cannot change the owner of {noun} **{entity.name}**: {refusal}"
        target = resolved.users[0]
        if str(entity.user_id or "") == target.id:
            lines.append(f"Owner unchanged: {target.label} already owns it.")
        else:
            try:
                changed = await apply_owner_transfer(
                    _db,
                    actor=caller,
                    entity=entity,
                    new_owner_id=target.id,
                    entity_type=entity_type,
                    entity_name=entity.name,
                )
            except HTTPException as exc:
                return updated, lines, f"Cannot change the owner of {noun} **{entity.name}**: {exc.detail}"
            if changed:
                updated.append("owner")
                lines.append(f"Owner is now {target.label}.")

    # 2. The people.
    if users_given or tier_given:
        if not users_given:
            return updated, lines, (
                "users_permission names the access given to the people in `users`; "
                "pass `users` too (emails or user ids)."
            )
        tier = (
            _CATALOG_SHARE_TIERS.get(str(users_permission).strip().lower())
            if tier_given
            else "read_only"
        )
        if tier is None:
            return updated, lines, (
                f"Invalid users_permission: use 'use' (attach and read the {noun}, the "
                "default) or 'manage' (also edit it)."
            )
        clear = isinstance(users, str) and users.strip().lower() in ("none", "clear")
        if clear and op != "replace":
            return updated, lines, (
                "'none'/'clear' removes every per-user share and is only valid with "
                "binding_mode='replace' (the default)."
            )
        entries = [] if clear else principal_entries(users)
        if not entries and not clear:
            return updated, lines, (
                "No valid users found in the input — provide one or more email addresses "
                "or user ids (a list, or comma-separated), or 'none'/'clear'."
            )
        if len(entries) > MAX_PRINCIPALS_PER_CALL:
            return updated, lines, (
                f"One call names at most {MAX_PRINCIPALS_PER_CALL} people - share with "
                "a group instead, or split the list."
            )
        if getattr(entity, "is_company_managed", False) and not getattr(entity, "access_restricted", False):
            return updated, lines, (
                f"{noun.capitalize()} **{entity.name}** is company-managed: everyone on the "
                "platform can already use it, so it is not shared with individual people. "
                "Nothing was changed."
            )
        resolved = await resolve_principals(_db, entries)
        refusal = resolved.refusal(granting=op != "remove")
        if refusal:
            return updated, lines, f"Cannot share {noun} **{entity.name}**: {refusal}"
        owner_id = str(entity.user_id) if entity.user_id else None
        owned = [p for p in resolved.users if p.id == owner_id]
        people = [p for p in resolved.users if p.id != owner_id]
        index = PrincipalIndex()
        index.add(resolved.users)
        current = stored_grants(entity.shared_user_ids, entity.shared_user_permissions)
        new_ids, new_tiers, delta = plan_grants(op, tier, current, people)
        if delta.changed:
            try:
                await apply_user_share_fields(
                    _db,
                    entity,
                    SharingChange(
                        shared_user_ids=new_ids or None,
                        shared_user_permissions=new_tiers or None,
                    ),
                    partial=True,
                )
            except HTTPException as exc:
                return updated, lines, f"Cannot share {noun} **{entity.name}**: {exc.detail}"
            updated.append("users")

        def _named(ids: list[str]) -> str:
            return ", ".join(index.user(key).label for key in ids)

        word = _share_tier_label(tier)
        if delta.added:
            lines.append(f"Shared with {_named(delta.added)} [{word}].")
        if delta.retiered:
            lines.append(f"Now {word}: {_named(delta.retiered)}.")
        if delta.removed:
            lines.append(f"Access removed: {_named(delta.removed)}.")
        if delta.unchanged:
            lines.append(f"Already {word}, unchanged: {_named(delta.unchanged)}.")
        if delta.not_shared:
            lines.append(f"Not shared with them, nothing to remove: {_named(delta.not_shared)}.")
        if owned:
            lines.append(f"Skipped, they own it: {', '.join(p.label for p in owned)}.")

    # 3. The groups.
    if groups_given:
        current_ids = [str(g) for g in entity.shared_group_ids or []]
        current_perms = dict(entity.shared_group_permissions or {})
        if group_ids == [] and shared_group_ids is not None and op != "replace":
            return updated, lines, (
                "'none'/'clear' unshares every group and is only valid with "
                "binding_mode='replace' (the default)."
            )
        final_ids, final_perms = group_ids, group_perms
        if op == "add":
            final_ids = list(dict.fromkeys([*current_ids, *(group_ids or [])]))
            final_perms = {**current_perms, **(group_perms or {})}
        elif op == "remove":
            gone = set(group_ids or [])
            final_ids = [g for g in current_ids if g not in gone]
            final_perms = {g: t for g, t in current_perms.items() if g in final_ids}
        refusal = await _apply_group_sharing(_db, entity, final_ids, final_perms)
        if refusal:
            return updated, lines, f"Cannot share {noun} **{entity.name}** with groups: {refusal}"
        after = [str(g) for g in entity.shared_group_ids or []]
        if after != current_ids or dict(entity.shared_group_permissions or {}) != current_perms:
            updated.append("groups")
            lines.append(f"Shared with {len(after)} group(s) now.")
        else:
            lines.append("Groups unchanged.")
    return updated, lines, None


def _binding_pairs(bindings: dict | None) -> set[tuple[str, str]]:
    """Every ``(provider, id)`` pair of an ``integration_bindings`` map."""
    return {
        (provider, str(iid))
        for provider, ids in (bindings or {}).items()
        for iid in (ids if isinstance(ids, list) else [ids])
    }


def _arg_provided(raw: object) -> bool:
    """Whether a verbatim tool arg should be processed (vs left unchanged).

    A real ``str`` counts as provided only when it is non-blank; a native
    non-string (forwarded JSON ``dict`` / number / bool) is always provided and
    handed to the field's parser; ``None`` is never provided. Used by the
    JSON-valued option guards so a ``(x or "").strip()`` pre-check can't raise
    ``AttributeError`` on a native ``dict`` (the very shape
    ``_parse_optional_json_arg`` is meant to accept).
    """
    if raw is None:
        return False
    if isinstance(raw, str):
        return bool(raw.strip())
    return True


def _parse_optional_json_arg(
    raw: object, label: str
) -> tuple[dict | None, str | None, bool]:
    """Parse a JSON-object argument that also accepts a clear sentinel.

    Returns ``(value, error, provided)``. Blank / ``None`` = leave unchanged.
    ``"none"`` / ``"clear"`` = clear to ``None`` (``value=None, provided=True``).
    A native ``dict`` (forwarded JSON) is accepted as-is; a string is parsed and
    must decode to a JSON object.

    NB: distinct from the nested ``_parse_json_object_arg`` closure (a 2-tuple
    ``(value, error)`` helper used by the create_* MCP/tool/integration tools);
    this 3-tuple variant adds the "leave unchanged" + "clear" semantics the
    update_agent option fields need.
    """
    if raw is None:
        return None, None, False
    if isinstance(raw, dict):
        return raw, None, True
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None, None, False
        if s.lower() in ("none", "clear"):
            return None, None, True
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            return (
                None,
                f"{label} must be a valid JSON object (or 'none'/'clear' to clear).",
                True,
            )
        if not isinstance(parsed, dict):
            return None, f"{label} must be a JSON object.", True
        return parsed, None, True
    return None, f"{label} must be a valid JSON object.", True


def _parse_wait_policy_arg(raw: object) -> tuple[dict | None, str | None, bool]:
    """Parse a ``wait_policy`` tool argument: ``(value, error, provided)``.

    Blank = leave unchanged; ``"none"`` / ``"clear"`` / ``{}`` = clear (inherit);
    a JSON object is validated by ``normalize_wait_policy_override``.
    """
    from app.tools.wait_policy import normalize_wait_policy_override

    value, error, provided = _parse_optional_json_arg(raw, "wait_policy")
    if error or not provided:
        return None, error, provided
    try:
        return normalize_wait_policy_override(value), None, True
    except ValueError as exc:
        return None, str(exc), True


def _validate_scoping_map(label: str, mapping: dict, *, is_bindings: bool) -> str | None:
    """Validate the per-key value shapes of an agent tool/element scoping map.

    Mirrors the REST ``UpdateAgentRequest`` Pydantic types so the builder can
    never persist a shape the endpoint would reject:
    ``dict[str, list[str] | None]`` for the ``*_allowed_tools`` maps and
    ``dict[str, list[dict]]`` for ``integration_element_bindings``. ``None``
    per-key values (= "all tools for that source") are expected to be stripped
    by the caller before validation, so every remaining value must be a list.

    Without this, a bare value such as ``{"github": "send_email"}`` would be
    stored verbatim and silently corrupt runtime filtering, where
    ``filter_integration_tools`` (``td.name not in allow_list``) and the MCP
    loader (``t.name in allow_list``) degrade to substring/character matching.

    Returns an LLM-facing error string, or ``None`` when the shape is valid.
    """
    for key, value in mapping.items():
        if not isinstance(value, list):
            kind = "objects" if is_bindings else "tool-name strings"
            return f"{label}['{key}'] must be a JSON array of {kind} (or null)."
        if is_bindings:
            if not all(isinstance(item, dict) for item in value):
                return f"{label}['{key}'] must be a list of JSON objects."
        elif not all(isinstance(item, str) for item in value):
            return f"{label}['{key}'] must be a list of tool-name strings."
    return None


def _registry_builtin_tool_ids() -> tuple[str, ...]:
    """Every built-in id an agent may carry, read from the registry.

    Each group of ``BUILTIN_TOOL_GROUPS`` (the ids the agent settings picker
    sets and ``get_agent`` reports), then each [DEPRECATED] former id of
    ``BUILTIN_GROUP_ALIASES`` and each renamed id of ``RENAMED_BUILTIN_GROUPS``
    (still accepted, stored as its group, never advertised). Derived, never
    hand-kept: a hand-kept tuple written before the built-in tools consolidation refused ``files``, ``web``, ``cti``,
    ``design`` and ``coordination``, so an API caller could not set them and
    a ``builtin_tool_ids`` write silently dropped them (#8827).
    """
    return tuple(
        dict.fromkeys([*(group.id for group in BUILTIN_TOOL_GROUPS), *BUILTIN_GROUP_ALIASES, *RENAMED_BUILTIN_GROUPS])
    )


# Built-in tool ids the platform recognizes: the single list update_agent,
# create_agent and duplicate_agent validate against and advertise.
# ``code_interpreter`` is still accepted even when globally disabled
# (DISABLE_CODE_INTERPRETER) — the runtime skips injecting it, exactly like the
# REST endpoint, which stores the id without gating on that flag.
_VALID_BUILTIN_TOOL_IDS: tuple[str, ...] = _registry_builtin_tool_ids()

# Ids the builder names to the model only while their feature flag is on. The
# validation keeps accepting them: an agent already carrying one stays editable,
# and the runtime withholds the tool itself.
_FEATURE_GATED_BUILTIN_TOOL_IDS: dict[str, FeatureFlag] = {
    "video_studio": "VIDEO_STUDIO",
    "custom_dashboards": "DASHBOARD_STUDIO",
    "phone": "MOBILE_COMPANION",
    "deep_investigation": "DEEP_INVESTIGATION",
}


def _builtin_group(tool_id: str) -> str:
    """*tool_id* as the group it stands for: a [DEPRECATED] former id names its group."""
    return alias_group(tool_id) or current_group_id(tool_id)


def _apply_builtin_binding_mode(current: list[str], given: list[str], mode: str) -> list[str]:
    """The built-in ids an agent carries once *given* is applied in *mode*.

    ``replace`` makes *given* the set; ``add`` appends each id whose group the
    agent does not already carry; ``remove`` drops every stored id whose group
    *given* names (a former id removes its group).
    """
    if mode == "add":
        carried = {_builtin_group(t) for t in current}
        out = list(current)
        for tool_id in given:
            if _builtin_group(tool_id) not in carried:
                carried.add(_builtin_group(tool_id))
                out.append(tool_id)
        return out
    if mode == "remove":
        dropped = {_builtin_group(t) for t in given}
        return [t for t in current if _builtin_group(t) not in dropped]
    return list(dict.fromkeys(given))


def _describe_builtin_change(before: list[str], after: list[str]) -> str:
    """The built-in groups a write added and removed, as one read-back sentence."""
    groups_before = list(dict.fromkeys(_builtin_group(t) for t in before))
    groups_after = list(dict.fromkeys(_builtin_group(t) for t in after))
    added = [g for g in groups_after if g not in groups_before]
    removed = [g for g in groups_before if g not in groups_after]
    parts = []
    if added:
        parts.append(f"added {', '.join(added)}")
    if removed:
        parts.append(f"removed {', '.join(removed)}")
    if not parts:
        return "Built-in tools unchanged."
    now = ", ".join(groups_after) or "none"
    return f"Built-in tools: {'; '.join(parts)} (now: {now})."


def _advertised_builtin_tool_ids() -> tuple[str, ...]:
    """``_VALID_BUILTIN_TOOL_IDS`` minus the former ids and the ids whose feature flag is off."""
    return tuple(
        tool_id
        for tool_id in _VALID_BUILTIN_TOOL_IDS
        if not is_former_builtin_id(tool_id)
        and (
            tool_id not in _FEATURE_GATED_BUILTIN_TOOL_IDS
            or is_feature_enabled(_FEATURE_GATED_BUILTIN_TOOL_IDS[tool_id])
        )
    )


async def _load_manageable_entity_for_builder(
    _db: AsyncSession,
    model: type,
    uid: uuid.UUID,
    entity_uuid: uuid.UUID,
    *,
    action: str = "modify",
    entity_label: str = "entity",
    lock: bool = False,
):
    """Load a three-tier-visible entity and verify the caller can mutate it.

    *lock* holds the row ``FOR UPDATE`` until the session commits and reads it
    afresh, for a write validated against what it stores.

    Generic counterpart of the REST API's ``get_manageable_entity`` for the
    agent-builder tools: it loads the entity through the same three-tier
    visibility filter (own + group-shared + company-managed, with the admin
    bypass of :func:`agent_tool_admin_bypass`), then applies ``can_manage``
    so company-managed entities stay admin-only and group-shared entities
    require ``config``-tier permission.

    Unlike ``get_manageable_entity`` it returns ``(entity, error_message)``
    instead of raising ``HTTPException`` so write tools can hand the friendly
    refusal straight back to the LLM. It also hides existence *more* strictly
    than ``get_manageable_entity``: that REST helper loads purely by id (no
    visibility filter) and returns 403 for a row that exists but isn't
    manageable — revealing its existence — whereas this helper applies the
    three-tier ``user_visible_filter`` first, so a not-found row and a
    not-visible row yield the same "not found or you don't have access"
    message and a probe cannot tell them apart. A visible-but-not-manageable
    row still gets the distinct "only admins…" / "no permission" message,
    matching the REST 403.

    Works for any model exposing the company-managed contract
    (``is_company_managed`` + ``user_id`` + ``shared_group_ids``): Prompt,
    AgentSkill, MCPServer, CustomTool, KnowledgeBase, Variable, Integration.
    """
    from app.api.v1.helpers import (
        can_manage_entity,
        get_user_group_ids,
        user_visible_filter,
    )

    user = await _load_caller_user_for_builder(_db, uid)
    if user is None:
        return None, "User not found."

    group_ids = await get_user_group_ids(_db, uid)
    query = sa_select(model).where(
        model.id == entity_uuid,
        user_visible_filter(model, uid, group_ids, is_admin=agent_tool_admin_bypass(user)),
    )
    if lock:
        query = query.with_for_update(of=model).execution_options(populate_existing=True)
    result = await _db.execute(query)
    entity = result.scalar_one_or_none()
    if entity is None:
        return None, (
            f"{entity_label} `{entity_uuid}` not found or you don't have access."
        )

    if not await can_manage_entity(_db, user, entity, group_ids):
        if getattr(entity, "is_company_managed", False):
            return None, (
                f"Only admins can {action} company-managed {entity_label}s."
            )
        return None, (
            f"You don't have permission to {action} this {entity_label}."
        )
    return entity, None


# The sections get_agent can be narrowed to, keyed by the heading that opens
# each block of its answer (#277). The identity block (name, id, slug, the
# created/updated stamps) is always kept so a projected read still names its
# agent; every other block is dropped unless asked for.
GET_AGENT_SECTIONS: dict[str, tuple[str, ...]] = {
    "behavior": ("Behavior:",),
    "model": ("Model:",),
    "tools": ("Tools & resources:",),
    "handover": ("Handover:",),
    "memory": ("Memory:",),
    "human_in_the_loop": ("Human-in-the-loop:", "Human-in-the-loop prompt (full):"),
    "channels": ("Channels & web:",),
    "sharing": ("Visibility & sharing:",),
    "guardrails": ("Guardrails (full):",),
    "intents": ("Intent bindings:",),
    "persona": ("Persona (full):",),
}


# The sections get_assignment can be narrowed to (#283), keyed by the heading
# that opens each block of its answer. The first block (name, id, agent,
# trigger and its config, every execution setting, schedule and delivery) is
# always kept: a read of the trigger or of the gates no longer carries the
# prompts whole.
GET_ASSIGNMENT_SECTIONS: dict[str, tuple[str, ...]] = {
    "gates": ("Gate stages (evaluated in order before the action prompt; a failed stage skips the run):",),
    "activation_prompt": (
        "Activation gate prompt (pre-filter deciding whether the assignment runs; empty = it always runs):",
    ),
    "prompt": ("Action prompt (the main instruction executed when the gate passes):",),
    "ai_label_prompt": ("AI label prompt (override):",),
    "human_in_the_loop_prompt": ("Human-in-the-loop prompt (override):",),
}


def parse_answer_sections(
    tool: str, sections: object, valid: dict[str, tuple[str, ...]], kept: str
) -> tuple[set[str] | None, str | None]:
    """Read a read tool's ``sections`` argument: ``(None, None)`` for the whole
    answer, the set of names asked for, or a refusal naming the valid ones."""
    if sections is None or sections == "" or sections == []:
        return None, None
    if isinstance(sections, str):
        names = [s.strip() for s in sections.split(",")]
    elif isinstance(sections, list) and all(isinstance(s, str) for s in sections):
        names = [s.strip() for s in sections]
    else:
        return None, "sections must be a list of section names."
    wanted = {n.lower() for n in names if n}
    unknown = sorted(wanted - set(valid))
    if unknown:
        return None, (
            f"Unknown {tool} section(s): {', '.join(unknown)}. "
            f"Valid sections: {', '.join(valid)} ({kept} is always shown)."
        )
    return wanted or None, None


def parse_get_agent_sections(sections: object) -> tuple[set[str] | None, str | None]:
    """Read get_agent's ``sections`` argument (see ``parse_answer_sections``)."""
    return parse_answer_sections("get_agent", sections, GET_AGENT_SECTIONS, "the identity block")


def parse_get_assignment_sections(sections: object) -> tuple[set[str] | None, str | None]:
    """Read get_assignment's ``sections`` argument (see ``parse_answer_sections``)."""
    return parse_answer_sections(
        "get_assignment", sections, GET_ASSIGNMENT_SECTIONS, "the settings block (trigger included)"
    )


def project_get_agent_sections(parts: list[str], wanted: set[str] | None) -> list[str]:
    """Keep get_agent's identity block and the blocks of ``wanted``."""
    return project_answer_sections(parts, wanted, GET_AGENT_SECTIONS)


def project_answer_sections(parts: list[str], wanted: set[str] | None, valid: dict[str, tuple[str, ...]]) -> list[str]:
    """Keep the blocks no section names and the blocks of ``wanted`` from a
    read tool's answer lines; blocks are separated by an empty line and opened
    by their heading (a multi-line value such as a prompt is one element)."""
    if wanted is None:
        return parts
    heading_to_section = {h: name for name, heads in valid.items() for h in heads}
    blocks: list[list[str]] = [[]]
    for line in parts:
        if line == "":
            blocks.append([])
        else:
            blocks[-1].append(line)
    kept: list[str] = []
    for block in blocks:
        if not block:
            continue
        section = heading_to_section.get(block[0])
        if section is None or section in wanted:
            if kept:
                kept.append("")
            kept.extend(block)
    return kept


def build_agent_builder_tool(
    db: AsyncSession,
    user_id: str,
    *,
    conversation_id: str | None = None,
    run_id: str | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the agent-builder tools.

    Provides a set of tools that allow an LLM to programmatically create agents
    and assignments within the platform.  This is a meta-tool:
    when enabled, users can ask an agent (or the General Assistant) to build
    new agents on their behalf through natural conversation.

    The tool set offers full CRUD parity with the REST API / UI for the
    entities a user manages day-to-day:

    * **Agents** – ``list_agents`` (paginated; admins may pass
      ``scope='all'`` for a platform-wide listing; ``detail='full'`` adds
      owner, created timestamp, integration bindings, knowledge bases, and
      exposure per agent for bulk audits without per-agent ``get_agent``
      round-trips), ``get_agent``,
      ``create_agent``, ``update_agent`` (name, persona, icon, output
      format, model, and all resource bindings), ``delete_agent``,
      ``list_agent_templates``.
    * **Assignments** – ``list_assignments``, ``get_assignment``,
      ``create_assignment``, ``update_assignment``, ``delete_assignment``,
      ``trigger_assignment``.
    * **Runs** – ``list_runs`` (paginated execution history, filterable by
      agent / assignment / status / search), ``get_run`` (full run detail).
      Visibility mirrors the REST runs API: own + company-managed runs;
      admins may pass ``scope='all'``.
    * **Knowledge bases** – ``list_knowledge_bases``, ``get_knowledge_base``,
      ``create_knowledge_base``, ``update_knowledge_base``,
      ``delete_knowledge_base``, ``add_kb_document``, ``list_kb_documents``,
      ``get_kb_document`` (exact de-chunked fetch by id), ``delete_kb_document``.
    * **Custom tools** – ``list_custom_tools``, ``get_custom_tool``,
      ``create_custom_tool``, ``update_custom_tool``, ``delete_custom_tool``.
    * **MCP servers** – ``list_mcp_servers``, ``get_mcp_server``,
      ``create_mcp_server``, ``update_mcp_server``, ``delete_mcp_server``.
    * **Integrations** – ``list_integrations``, ``list_integration_catalog``,
      ``get_integration``, ``create_integration``, ``update_integration``,
      ``delete_integration``.
    * **Prompts** – ``list_prompts``, ``get_prompt``, ``create_prompt``,
      ``update_prompt``, ``delete_prompt``.
    * **Skills** – ``list_skills``, ``get_skill``, ``create_skill``,
      ``update_skill``, ``delete_skill``.
    * **Variables** – ``list_variables``, ``get_variable``,
      ``create_variable``, ``update_variable``, ``delete_variable``.
    * **Workspaces** (``builtin_tools/workspaces.py``) –
      ``list_workspaces``, ``get_workspace``, ``create_workspace``,
      ``update_workspace`` (incl. archive / restore), ``delete_workspace``,
      ``set_default_workspace``, ``update_workspace_knowledge_bases``, and the
      caller's own conversations: ``list_conversations``,
      ``move_conversations_to_workspace``. *conversation_id* is the
      conversation the turn runs in, which the move files when no id is named.
      *run_id* is the run the turn belongs to, if any: the parent of the run
      ``run_agent_once`` creates.
    * **Agent accounts** (``builtin_tools/agent_accounts.py``) –
      ``list_agent_accounts``, ``get_agent_account``,
      ``list_agent_account_options``, ``create_agent_account``,
      ``update_agent_account`` (incl. enable / disable),
      ``edit_agent_account_rule``, ``delete_agent_account``,
      ``preview_agent_account_decision``, ``list_agent_account_decisions``;
      every write through ``services/agent_accounts/management.py``.
    * **Intent bindings** (``builtin_tools/intent_bindings.py``) -
      ``list_agent_intent_bindings``, ``bind_agent_to_intent``,
      ``unbind_agent_from_intent``; every write through
      ``services/intent_binding_writes.py``, the REST routes' writer.
    * **Knowledge base sync sources** (``builtin_tools/kb_sync_sources.py``) -
      ``get_knowledge_base_sources``, ``update_knowledge_base_source``,
      ``resync_knowledge_base``; every call through the REST handlers of
      ``/knowledge-bases/{id}/sync-sources``.
    * **Discovery** – ``list_available_resources``.

    Every write tool enforces the same three-tier permission gates as the
    REST API so the LLM can never bypass UI-level restrictions:

    * Built-in agents / entities are read-only in XTM One mode.
    * Company-managed entities are read-only for non-admins.
    * Group-shared entities require ``config``-tier permission to mutate.

    Feature flags are honoured too: ``DISABLE_CUSTOM_TOOLS`` and
    ``DISABLE_CUSTOM_VARIABLES`` make the corresponding write tools refuse
    gracefully. Secret material (variable values, MCP env/headers, and
    integration credentials) is encrypted at rest and never echoed back in a
    tool result.
    """

    executor = CallableToolExecutor()

    async def _id_arg(value: object, arg_name: str) -> tuple[uuid.UUID | None, str | None]:
        """:func:`_validate_id_arg`, with an id prefix (8+ hexadecimal
        characters) of an agent, an assignment, a skill, a run or an
        integration the caller can see taken for the full id it starts (``builtin_tools/id_prefix.py``);
        a prefix starting several is refused with the candidates named, and
        a prefix starting none is answered "not found" like a full id of an
        object the caller cannot see (deleted, or never there), never as a
        malformed argument."""
        from app.api.v1.helpers import (
            assignment_visible_filter,
            caller_visible_filter,
            get_user_group_ids,
            run_visible_filter,
        )
        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.agent_skill import AgentSkill
        from app.models.assignment import Assignment, AssignmentRun
        from app.models.integration import Integration

        from .id_prefix import id_prefix, resolve_id_prefix

        if id_prefix(value) is None:
            return _validate_id_arg(value, arg_name)
        uid = safe_uuid(user_id) if user_id else None
        model: Any
        visible: Any
        label: Any = None
        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            if arg_name == "run_id":
                # get_run's own rule: own and company-managed runs, every
                # run for an admin (agent_tool_admin_bypass).
                if uid is None:
                    return _validate_id_arg(value, arg_name)
                caller = await _load_caller_user_for_builder(_db, uid)
                visible = AssignmentRun.id.is_not(None) if agent_tool_admin_bypass(caller) else run_visible_filter(uid)
                model, noun, entity, label = AssignmentRun, "runs", "Run", AssignmentRun.status
            elif arg_name == "integration_id":
                model, noun, entity = Integration, "integrations", "Integration"
                visible = caller_visible_filter(Integration, uid, group_ids)
            elif arg_name == "assignment_id":
                caller = await _load_caller_user_for_builder(_db, uid) if uid else None
                visible = assignment_visible_filter(uid, group_ids, is_admin=agent_tool_admin_bypass(caller))
                model, noun, entity = Assignment, "assignments", "Assignment"
            elif arg_name == "skill_id":
                model, noun, entity = AgentSkill, "skills", "Skill"
                visible = caller_visible_filter(AgentSkill, uid, group_ids)
            else:
                model, noun, entity = Agent, "agents", "Agent"
                visible = caller_visible_filter(Agent, uid, group_ids)
            full, ambiguous = await resolve_id_prefix(_db, model, value, [visible], noun=noun, label=label)
        if ambiguous:
            return None, ambiguous
        if safe_uuid(full) is None:
            return None, (
                f"{entity} `{str(value).strip()}` not found or you don't have access: "
                f"no {noun[:-1]} you can see has an id starting with it."
            )
        return _validate_id_arg(full, arg_name)

    # ── list_agent_templates ──────────────────────────────────────────

    async def _list_agent_templates() -> str:
        from sqlalchemy import select

        from app.core.database import async_session as _builder_session
        from app.models.agent_template import AgentTemplate

        async with _builder_session() as _db:
            result = await _db.execute(
                select(AgentTemplate)
                .where(AgentTemplate.enabled.is_(True))
                .order_by(AgentTemplate.name)
            )
            templates = result.scalars().all()
            if not templates:
                return "No agent templates available."

            parts: list[str] = ["Available agent templates:\n"]
            for t in templates:
                required = (
                    f"  Required integrations: {', '.join(t.required_integrations)}"
                    if t.required_integrations
                    else ""
                )
                assignments = (
                    f"  Default assignments: {len(t.default_assignments or [])}"
                    if t.default_assignments
                    else ""
                )
                parts.append(
                    f"• **{t.name}** (slug: `{t.slug}`, category: {t.category})\n"
                    f"  {t.description or 'No description'}\n"
                    f"{required}{assignments}"
                )
            return "\n".join(parts)

    # ── list_available_resources ──────────────────────────────────────

    async def _list_available_resources(resource_type: str = "all") -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.custom_tool import CustomTool
        from app.models.integration import Integration
        from app.models.knowledge_base import KnowledgeBase
        from app.models.mcp_server import MCPServer

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            # Anonymous users: the company-wide tier (caller_visible_filter)
            uid = None
        async with _builder_session() as _db:
            parts: list[str] = []

            # Resolve group memberships for three-tier visibility
            group_ids = await get_user_group_ids(_db, uid) if uid else []

            if resource_type in ("all", "integrations"):
                q = (
                    select(Integration)
                    .where(
                        caller_visible_filter(Integration, uid, group_ids),
                        Integration.status == "connected",
                    )
                    .order_by(Integration.name)
                )
                r = await _db.execute(q)
                integrations = r.scalars().all()
                if integrations:
                    parts.append("**Connected Integrations:**")
                    for i in integrations:
                        cm = " [company-managed]" if i.is_company_managed else ""
                        parts.append(
                            f"  • {i.name} (id: `{i.id}`, provider: {i.provider}){cm}"
                        )
                else:
                    parts.append("**Connected Integrations:** None")

            if resource_type in ("all", "knowledge_bases"):
                q = (
                    select(KnowledgeBase)
                    .where(
                        caller_visible_filter(KnowledgeBase, uid, group_ids),
                    )
                    .order_by(KnowledgeBase.name)
                )
                r = await _db.execute(q)
                kbs = r.scalars().all()
                if kbs:
                    parts.append("\n**Knowledge Bases:**")
                    for kb in kbs:
                        cm = " [company-managed]" if kb.is_company_managed else ""
                        parts.append(f"  • {kb.name} (id: `{kb.id}`){cm}")
                else:
                    parts.append("\n**Knowledge Bases:** None")

            if resource_type in ("all", "mcp_servers"):
                q = (
                    select(MCPServer)
                    .where(
                        caller_visible_filter(MCPServer, uid, group_ids),
                        MCPServer.enabled.is_(True),
                    )
                    .order_by(MCPServer.name)
                )
                r = await _db.execute(q)
                mcps = r.scalars().all()
                if mcps:
                    parts.append("\n**MCP Servers:**")
                    for srv in mcps:
                        cm = " [company-managed]" if srv.is_company_managed else ""
                        parts.append(
                            f"  • {srv.name} (id: `{srv.id}`, transport: {srv.transport}){cm}"
                        )
                else:
                    parts.append("\n**MCP Servers:** None")

            if resource_type in ("all", "custom_tools"):
                q = (
                    select(CustomTool)
                    .where(
                        caller_visible_filter(CustomTool, uid, group_ids),
                        CustomTool.enabled.is_(True),
                    )
                    .order_by(CustomTool.name)
                )
                r = await _db.execute(q)
                tools = r.scalars().all()
                if tools:
                    parts.append("\n**Custom Tools:**")
                    for ct in tools:
                        cm = " [company-managed]" if ct.is_company_managed else ""
                        parts.append(f"  • {ct.name} (id: `{ct.id}`){cm}")
                else:
                    parts.append("\n**Custom Tools:** None")

            return "\n".join(parts) if parts else "No resources found."

    # ── create_agent ──────────────────────────────────────────────────

    async def _create_agent(
        name: str,
        description: str = "",
        persona: str = "",
        template_slug: str = "",
        icon: str = "Bot",
        knowledge_base_ids: str = "",
        knowledge_base_permissions: str = "",
        mcp_server_ids: str = "",
        custom_tool_ids: str = "",
        integration_bindings: str = "",
        create_default_assignments: bool = True,
        tags: str = "",
        is_company_managed: bool = False,
        max_iterations: int | None = None,
        # Every other field update_agent accepts, applied through it
        # once the agent exists so the two can never validate differently.
        slug: str = "",
        guardrails: str = "",
        enabled: str = "",
        output_format: str = "",
        output_schema: str = "",
        llm_model: str = "",
        llm_provider: str = "",
        llm_provider_id: str = "",
        llm_model_source: str = "",
        temperature: str = "",
        reasoning_effort: str = "",
        organization_context_mode: str = "",
        organization_context_categories: object = None,
        builtin_tool_ids: str = "",
        tool_deny_list: str = "",
        prompt_ids: str = "",
        skill_ids: str = "",
        integration_template_bindings: str = "",
        integration_allowed_tools: str = "",
        mcp_allowed_tools: str = "",
        builtin_allowed_tools: str = "",
        integration_element_bindings: str = "",
        integration_element_enforcement: str = "",
        integration_read_only: str = "",
        integration_template_read_only: str = "",
        integration_signatures: object = "",
        integration_live_writes: object = "",
        handover_agent_ids: str = "",
        handover_types: str = "",
        handover_conditions: str = "",
        allow_self_fanout: str = "",
        accept_caller_output_format: str = "",
        prepare_tool_ref: str = "",
        finalize_tool_ref: str = "",
        skip_llm: str = "",
        memory_short_term_enabled: str = "",
        memory_medium_term_enabled: str = "",
        memory_persistent_enabled: str = "",
        memory_user_enabled: str = "",
        human_in_the_loop: str = "",
        human_in_the_loop_prompt: str = "",
        available_channel_types: str = "",
        channel_config: str = "",
        web_enabled: str = "",
        web_suggested_prompts: str = "",
        is_public_catalog: str = "",
        catalog_include_personal_tools: str = "",
        disable_chat: str = "",
        shared_group_ids: str = "",
        shared_group_permissions: str = "",
        users: object = "",
        users_permission: str = "",
        config: str = "",
        wait_policy: object = "",
    ) -> str:
        _configured = {
            name: value
            for name, value in {
                "slug": slug,
                "guardrails": guardrails,
                "enabled": enabled,
                "output_format": output_format,
                "output_schema": output_schema,
                "llm_model": llm_model,
                "llm_provider": llm_provider,
                "llm_provider_id": llm_provider_id,
                "llm_model_source": llm_model_source,
                "temperature": temperature,
                "reasoning_effort": reasoning_effort,
                "organization_context_mode": organization_context_mode,
                "organization_context_categories": organization_context_categories,
                "builtin_tool_ids": builtin_tool_ids,
                "tool_deny_list": tool_deny_list,
                "prompt_ids": prompt_ids,
                "skill_ids": skill_ids,
                "integration_template_bindings": integration_template_bindings,
                "integration_allowed_tools": integration_allowed_tools,
                "mcp_allowed_tools": mcp_allowed_tools,
                "builtin_allowed_tools": builtin_allowed_tools,
                "integration_element_bindings": integration_element_bindings,
                "integration_element_enforcement": integration_element_enforcement,
                "integration_read_only": integration_read_only,
                "integration_template_read_only": integration_template_read_only,
                "integration_signatures": integration_signatures,
                "integration_live_writes": integration_live_writes,
                "handover_agent_ids": handover_agent_ids,
                "handover_types": handover_types,
                "handover_conditions": handover_conditions,
                "allow_self_fanout": allow_self_fanout,
                "accept_caller_output_format": accept_caller_output_format,
                "prepare_tool_ref": prepare_tool_ref,
                "finalize_tool_ref": finalize_tool_ref,
                "skip_llm": skip_llm,
                "memory_short_term_enabled": memory_short_term_enabled,
                "memory_medium_term_enabled": memory_medium_term_enabled,
                "memory_persistent_enabled": memory_persistent_enabled,
                "memory_user_enabled": memory_user_enabled,
                "human_in_the_loop": human_in_the_loop,
                "human_in_the_loop_prompt": human_in_the_loop_prompt,
                "available_channel_types": available_channel_types,
                "channel_config": channel_config,
                "web_enabled": web_enabled,
                "web_suggested_prompts": web_suggested_prompts,
                "is_public_catalog": is_public_catalog,
                "catalog_include_personal_tools": catalog_include_personal_tools,
                "disable_chat": disable_chat,
                "shared_group_ids": shared_group_ids,
                "shared_group_permissions": shared_group_permissions,
                "users": users,
                "users_permission": users_permission,
                "config": config,
                "wait_policy": wait_policy,
            }.items()
            if _arg_provided(value)
        }
        created = await _create_agent_row(
            name=name,
            description=description,
            persona=persona,
            template_slug=template_slug,
            icon=icon,
            knowledge_base_ids=knowledge_base_ids,
            knowledge_base_permissions=knowledge_base_permissions,
            mcp_server_ids=mcp_server_ids,
            custom_tool_ids=custom_tool_ids,
            integration_bindings=integration_bindings,
            create_default_assignments=create_default_assignments,
            tags=tags,
            is_company_managed=is_company_managed,
            max_iterations=max_iterations,
        )
        if isinstance(created, str):
            return created
        agent_id, result_parts = created
        if _configured:
            _configuring = _CONFIGURING_NEW_AGENT.set(True)
            try:
                configured = await _update_agent(agent_id=agent_id, **_configured)
            finally:
                _CONFIGURING_NEW_AGENT.reset(_configuring)
            if not isinstance(configured, _AgentUpdated):
                deleted = await _delete_agent(agent_id)
                if not isinstance(deleted, _AgentDeleted):
                    return (
                        f"Agent `{agent_id}` was created but configuring it failed: "
                        f"{configured} It could not be removed ({deleted}); fix the "
                        "field with update_agent or delete the agent."
                    )
                return f"Agent not created: {configured}"
            result_parts.append(f"  • Configured: {', '.join(configured.fields)}")
        slug_now, binding_lines = await _describe_agent_bindings(agent_id)
        if slug_now:
            result_parts = [
                f"  • Slug: `{slug_now}`" if part.startswith("  • Slug:") else part
                for part in result_parts
            ]
        result_parts.extend(binding_lines)
        result_parts.extend(await _suggested_skill_lines(agent_id))
        result_parts.append("\nThe agent is now live and accessible in the platform.")
        return "\n".join(result_parts)

    async def _suggested_skill_lines(agent_id: str) -> list[str]:
        """The craft skills an agent created with no skill would use well, never attached.

        One line per skill of ``agent_template_skills.suggested_skill_slugs``
        for the built-in groups the agent carries, resolved to the skills the
        caller may attach; nothing when the agent already has a skill.
        """
        from sqlalchemy import select

        from app.api.v1.helpers import get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.agent_skill import AgentSkill
        from app.services.agent_template_skills import (
            suggested_skill_slugs,
            visible_skill_ids_by_slug,
        )

        uid = safe_uuid(user_id)
        if uid is None:
            return []
        try:
            async with _builder_session() as _db:
                agent = (await _db.execute(select(Agent).where(Agent.id == uuid.UUID(agent_id)))).scalar_one_or_none()
                if agent is None or agent.skill_ids:
                    return []
                slugs = suggested_skill_slugs(agent.builtin_tool_ids)
                if not slugs:
                    return []
                caller = await _load_caller_user_for_builder(_db, uid)
                by_slug = await visible_skill_ids_by_slug(
                    _db,
                    slugs,
                    user_id=uid,
                    user_group_ids=await get_user_group_ids(_db, uid),
                    is_admin=agent_tool_admin_bypass(caller),
                )
                ids = [by_slug[s] for s in slugs if s in by_slug]
                if not ids:
                    return []
                names = {
                    str(row[0]): row[1]
                    for row in (
                        await _db.execute(
                            select(AgentSkill.id, AgentSkill.name).where(AgentSkill.id.in_([uuid.UUID(i) for i in ids]))
                        )
                    ).all()
                }
        except Exception:
            # A suggestion is a courtesy: the agent exists whatever this read does.
            logger.warning("Could not suggest skills for agent %s", agent_id, exc_info=True)
            return []
        lines = ["  • Suggested skills for its built-in tools (not attached; add them with update_agent skill_ids):"]
        lines.extend(f"    - {names.get(i, i)} (`{i}`)" for i in ids)
        return lines

    async def _describe_agent_bindings(agent_id: str) -> tuple[str | None, list[str]]:
        """The agent's slug, and each integration binding as ``provider:id (name)``.

        Counted per binding, not per provider, so two accounts of one provider
        read as two, and named so the caller can check every one it passed.
        """
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.integration import Integration
        from app.services.integration_read_only import read_only_integration_ids

        uid = safe_uuid(user_id)
        try:
            async with _builder_session() as _db:
                agent = (
                    await _db.execute(select(Agent).where(Agent.id == uuid.UUID(agent_id)))
                ).scalar_one_or_none()
                if agent is None:
                    return None, []
                slug_now = agent.slug
                pairs = [
                    (provider, str(iid))
                    for provider in sorted(agent.integration_bindings or {})
                    for iid in (agent.integration_bindings or {})[provider] or []
                ]
                if not pairs:
                    return slug_now, []
                group_ids = await get_user_group_ids(_db, uid) if uid else []
                rows = await _db.execute(
                    select(Integration.id, Integration.name).where(
                        Integration.id.in_([u for _, i in pairs if (u := safe_uuid(i))]),
                        caller_visible_filter(Integration, uid, group_ids),
                    )
                )
                names = {str(row[0]): row[1] for row in rows.all()}
                read_only = read_only_integration_ids(agent)
        except Exception:
            # Describing is a courtesy: the agent exists whatever this read does.
            logger.warning("Could not describe the bindings of agent %s", agent_id, exc_info=True)
            return None, []
        lines = [
            f"  • Integrations: {len(pairs)} binding{'' if len(pairs) == 1 else 's'}"
        ]
        for provider, iid in pairs:
            label = f"    - {provider}:{iid}"
            if iid in names:
                label += f" ({names[iid]})"
            if iid in read_only:
                label += " [read-only]"
            lines.append(label)
        return slug_now, lines

    async def _create_agent_row(
        *,
        name: str,
        description: str,
        persona: str,
        template_slug: str,
        icon: str,
        knowledge_base_ids: str,
        knowledge_base_permissions: str,
        mcp_server_ids: str,
        custom_tool_ids: str,
        integration_bindings: str,
        create_default_assignments: bool,
        tags: str,
        is_company_managed: bool,
        max_iterations: int | None,
    ) -> tuple[str, list[str]] | str:
        """Create the agent row (and its template assignments).

        Returns ``(agent_id, result lines)``, or the refusal to hand back.
        """
        from app.core.database import async_session as _builder_session
        from app.core.utils import slugify
        from app.models.agent import Agent
        from app.models.agent_template import (
            AgentTemplate,
            default_assignment_manual_parameters,
            default_assignment_text,
            default_assignment_trigger_config,
        )
        from app.models.assignment import Assignment
        from app.models.general_assistant_config import (
            MAX_ITERATIONS_CEILING,
            clamp_max_iterations,
        )

        try:
            try:
                uid = uuid.UUID(user_id)
            except (ValueError, AttributeError):
                return "Agent creation is not available for anonymous users."

            name = (name or "").strip()
            if not name:
                return "Agent name is required (whitespace-only names are not allowed)."

            # max_iterations: None = inherit the platform default. A real value
            # is validated + clamped to the shared ceiling (mirrors the REST
            # create endpoint). A bool is never a valid limit.
            if max_iterations is not None:
                if not isinstance(max_iterations, int) or isinstance(
                    max_iterations, bool
                ):
                    return (
                        "max_iterations must be an integer (or omitted to use "
                        "the platform default)."
                    )
                if max_iterations < 1 or max_iterations > MAX_ITERATIONS_CEILING:
                    return (
                        f"max_iterations must be between 1 and "
                        f"{MAX_ITERATIONS_CEILING}."
                    )

            async with _builder_session() as _db:
                # Creating a company-managed (platform-wide) agent is admin-only,
                # mirroring the REST create endpoint and the other create_*
                # builder tools (create_prompt, create_custom_tool, ...).
                if is_company_managed:
                    caller = await _load_caller_user_for_builder(_db, uid)
                    if caller is None or not caller.is_admin:
                        return "Only admins can create company-managed agents."

                # Parse + validate comma-separated UUID id lists. Each id is
                # canonicalised via ``str(uuid.UUID(...))`` and a non-UUID token
                # is rejected up front with a clear error, mirroring
                # ``update_agent`` — otherwise a bogus id would persist a dead
                # binding and later raise ``ValueError`` in the downstream
                # ``uuid.UUID(id)`` conversions during integration/tool loading.
                def _csv_uuid_list(
                    raw: str, label: str
                ) -> tuple[list[str] | None, str | None]:
                    ids: list[str] = []
                    invalid: list[str] = []
                    for token in raw.split(","):
                        token = token.strip()
                        if not token:
                            continue
                        try:
                            ids.append(str(uuid.UUID(token)))
                        except ValueError:
                            invalid.append(token)
                    if invalid:
                        return None, (
                            f"Invalid {label}: {', '.join(invalid)} — each id must "
                            "be a valid UUID (comma-separated)."
                        )
                    return ids, None

                kb_ids, _err = _csv_uuid_list(knowledge_base_ids, "knowledge_base_ids")
                if _err:
                    return _err
                # Per-KB write access (mirrors update_agent + the REST create
                # endpoint). Read is the default; only "write" grants are kept,
                # and grants for unbound KBs are pruned after construction.
                kb_perms, _perm_err, _ = _parse_kb_permissions_arg(
                    knowledge_base_permissions
                )
                if _perm_err:
                    return _perm_err
                mcp_ids, _err = _csv_uuid_list(mcp_server_ids, "mcp_server_ids")
                if _err:
                    return _err
                ct_ids, _err = _csv_uuid_list(custom_tool_ids, "custom_tool_ids")
                if _err:
                    return _err
                tag_list = (
                    [x.strip() for x in tags.split(",") if x.strip()] if tags else []
                )

                # Parse + validate integration bindings
                # (format: "provider:id,provider:id"). Mirrors ``update_agent``:
                # every pair is validated, the provider key is canonicalised to
                # lower-case (so it matches ``Integration.provider`` at tool-load
                # time), and the integration_id is canonicalised via
                # ``str(uuid.UUID(...))``. A malformed pair (missing ``:`` or a
                # non-UUID id) is rejected with a clear error rather than silently
                # dropped, so a partial/broken binding can never be persisted.
                bindings: dict[str, list[str]] = {}
                if integration_bindings:
                    malformed: list[str] = []
                    for raw_pair in integration_bindings.split(","):
                        cleaned = raw_pair.strip()
                        if not cleaned:
                            continue
                        prov, sep, integ_id = cleaned.partition(":")
                        prov, integ_id = prov.strip().lower(), integ_id.strip()
                        if not sep or not prov or not integ_id:
                            malformed.append(cleaned)
                            continue
                        try:
                            integ_id = str(uuid.UUID(integ_id))
                        except ValueError:
                            malformed.append(cleaned)
                            continue
                        bindings.setdefault(prov, []).append(integ_id)
                    if malformed:
                        return (
                            "Invalid integration_bindings entry(ies): "
                            f"{', '.join(malformed)}. Use 'provider:integration_id' "
                            "pairs (integration_id must be a valid UUID) separated "
                            "by commas (e.g. 'github:UUID,slack:UUID')."
                        )

                # Company-managed agents can only bind company-managed
                # resources (mirrors the REST create endpoint). Validate before
                # persisting so a mixed binding fails fast with a clear,
                # actionable error instead of the generic create failure below.
                if is_company_managed:
                    from fastapi import HTTPException

                    from app.services.agent_bindings import validate_company_managed_bindings

                    try:
                        await validate_company_managed_bindings(
                            _db,
                            mcp_server_ids=mcp_ids or None,
                            knowledge_base_ids=kb_ids or None,
                            integration_bindings=bindings or None,
                            handover_agent_ids=None,
                        )
                    except HTTPException as exc:
                        return str(exc.detail)

                # Personal (non-company-managed) agent: mirror the REST create
                # endpoint's personal-binding visibility gate
                # (``validate_personal_agent_bindings``) so a non-admin cannot
                # bind another user's private integration (and hijack its OAuth
                # token / API key), KB, MCP server, or custom tool by guessing a
                # UUID. Only runs when a binding is actually provided (an unbound
                # create has nothing to validate). Admins bypass, exactly like
                # REST, unless AGENT_TOOLS_STRICT_ADMIN_ACCESS. skill_ids /
                # prompt_ids / handover_agent_ids are not settable at create
                # time, so only the four create bindings are checked here. This
                # closes the create-path half of the same gap update_agent
                # already guards.
                elif kb_ids or mcp_ids or ct_ids or bindings:
                    from fastapi import HTTPException

                    from app.api.v1.helpers import get_user_group_ids
                    from app.services.agent_bindings import validate_personal_agent_bindings

                    _pb_caller = await _load_caller_user_for_builder(_db, uid)
                    _pb_group_ids = await get_user_group_ids(_db, uid)
                    try:
                        await validate_personal_agent_bindings(
                            _db,
                            uid,
                            _pb_group_ids,
                            is_admin=agent_tool_admin_bypass(_pb_caller),
                            mcp_server_ids=mcp_ids or None,
                            knowledge_base_ids=kb_ids or None,
                            integration_bindings=bindings or None,
                            handover_agent_ids=None,
                            custom_tool_ids=ct_ids or None,
                            skill_ids=None,
                            prompt_ids=None,
                        )
                    except HTTPException as exc:
                        return str(exc.detail)

                # Generate unique slug
                from app.api.v1.helpers import fit_agent_slug

                base_slug = slugify(name)
                slug = fit_agent_slug(base_slug)
                from sqlalchemy import select as sa_select

                counter = 0
                while True:
                    exists = await _db.execute(
                        sa_select(Agent.id).where(Agent.slug == slug).limit(1)
                    )
                    if not exists.scalar_one_or_none():
                        break
                    counter += 1
                    slug = fit_agent_slug(base_slug, f"-{counter}")

                # Load template if specified
                template = None
                template_persona = ""
                if template_slug:
                    t_result = await _db.execute(
                        sa_select(AgentTemplate).where(
                            AgentTemplate.slug == template_slug,
                            AgentTemplate.enabled.is_(True),
                        )
                    )
                    template = t_result.scalar_one_or_none()
                    if template:
                        template_persona = template.default_persona or ""

                # The tool takes no skill list at creation: an agent made from
                # a template starts with the template's default skills the
                # caller may attach, as the REST create does when the request
                # omits ``skill_ids``.
                template_skill_ids: list[str] = []
                if template is not None and template.default_skills:
                    from app.api.v1.helpers import get_user_group_ids
                    from app.services.agent_template_skills import (
                        template_default_skill_ids,
                    )

                    _ts_caller = await _load_caller_user_for_builder(_db, uid)
                    template_skill_ids = await template_default_skill_ids(
                        _db,
                        template.default_skills,
                        user_id=uid,
                        user_group_ids=await get_user_group_ids(_db, uid),
                        is_admin=agent_tool_admin_bypass(_ts_caller),
                    )

                # Create the agent. The owner (user_id) is always the creator,
                # even for company-managed agents — visibility is controlled by
                # ``is_company_managed`` alone (mirrors the REST endpoint).
                agent = Agent(
                    user_id=uid,
                    name=name,
                    slug=slug,
                    description=(description or "").strip() or None,
                    icon=(icon or "").strip() or "Bot",
                    persona=(persona or "").strip() or template_persona or None,
                    template_slug=template_slug or None,
                    tags=tag_list or [],
                    knowledge_base_ids=kb_ids or [],
                    knowledge_base_permissions=kb_perms or None,
                    mcp_server_ids=mcp_ids or [],
                    custom_tool_ids=ct_ids or [],
                    skill_ids=template_skill_ids,
                    integration_bindings=bindings or {},
                    is_company_managed=is_company_managed,
                    max_iterations=(
                        clamp_max_iterations(max_iterations)
                        if max_iterations is not None
                        else None
                    ),
                    # Web chat only, as the REST create: external channels
                    # are opted into (channel_config / available_channel_types).
                    available_channel_types=["web"],
                    enabled=True,
                )
                # Drop any write grant whose KB wasn't also bound above.
                agent.prune_orphaned_knowledge_base_permissions()
                _db.add(agent)
                await _db.flush()
                # The history every other way of creating an agent seeds.
                from app.services.agent_config_versioning import seed_initial_agent_versions

                await seed_initial_agent_versions(_db, agent=agent, author_id=uid)

                # Auto-create assignments from template
                assignment_count = 0
                lost_trigger_integrations = 0
                if create_default_assignments and template and template.default_assignments:
                    from types import SimpleNamespace

                    from app.api.v1.binding_access import trigger_config_for_copy
                    from app.core.utils import ensure_webhook_secret

                    _tmpl_caller = await _load_caller_user_for_builder(_db, uid)
                    for wf_def in template.default_assignments:
                        wf_trigger_type = default_assignment_text(
                            template_slug, wf_def, "trigger_type", "manual"
                        )
                        wf_trigger_config, lost = await trigger_config_for_copy(
                            _db,
                            user=_tmpl_caller or SimpleNamespace(id=uid, is_admin=False),
                            trigger_config=default_assignment_trigger_config(wf_def),
                            is_admin=agent_tool_admin_bypass(_tmpl_caller),
                        )
                        if lost:
                            lost_trigger_integrations += 1
                        wf = Assignment(
                            user_id=uid,
                            agent_id=agent.id,
                            # Mirror the agent's visibility onto its assignments
                            # so a company-managed agent's automations are shared
                            # too (parity with the REST create + the toggle).
                            is_company_managed=is_company_managed,
                            name=default_assignment_text(
                                template_slug, wf_def, "name", "Assignment"
                            ),
                            description=wf_def.get("description"),
                            trigger_type=wf_trigger_type,
                            # Webhook assignments are born with a fresh
                            # secret like API-created ones (SEC-5), and match
                            # their event exactly like any new trigger.
                            trigger_config=ensure_webhook_secret(
                                wf_trigger_type,
                                new_trigger_config(wf_trigger_type, wf_trigger_config),
                            ),
                            prompt=wf_def.get("prompt", ""),
                            ai_label=wf_def.get("ai_label", True),
                            human_in_the_loop=wf_def.get("human_in_the_loop", True),
                            delay=default_assignment_text(
                                template_slug, wf_def, "delay", None
                            ),
                            use_knowledge_bases=wf_def.get(
                                "use_knowledge_bases", True
                            ),
                            # null = inherit (agent override -> platform
                            # default). Clamp a real value to the shared
                            # ceiling, mirroring the REST create path
                            # (templates can carry per-assignment
                            # budgets and every instantiation path must
                            # honour them). Free-form template JSON: a
                            # non-int value (string/bool/explicit null)
                            # falls back to the default instead of
                            # crashing agent creation.
                            max_iterations=(
                                clamp_max_iterations(_wf_mi)
                                if isinstance(
                                    _wf_mi := wf_def.get("max_iterations"), int
                                )
                                and not isinstance(_wf_mi, bool)
                                else None
                            ),
                            timeout_seconds=(
                                _wf_ts
                                if isinstance(
                                    _wf_ts := wf_def.get("timeout_seconds"), int
                                )
                                and not isinstance(_wf_ts, bool)
                                else 300
                            ),
                            manual_trigger_parameters=default_assignment_manual_parameters(
                                wf_def
                            ),
                            enabled=wf_def.get("enabled", True),
                        )
                        _db.add(wf)
                        assignment_count += 1

                await _db.commit()

                result_parts = [
                    f"Agent **{name}** created successfully!",
                    f"  • ID: `{agent.id}`",
                    f"  • Slug: `{slug}`",
                ]
                if template:
                    result_parts.append(f"  • Template: {template.name}")
                if assignment_count:
                    result_parts.append(
                        f"  • Default assignments created: {assignment_count}"
                    )
                if lost_trigger_integrations:
                    one = lost_trigger_integrations == 1
                    result_parts.append(
                        f"  • {lost_trigger_integrations} assignment trigger "
                        f"integration{'' if one else 's'} of the template "
                        f"{'was' if one else 'were'} not copied: you cannot use "
                        f"{'it' if one else 'them'}. "
                        f"{'That trigger reads' if one else 'Those triggers read'} "
                        "the agent's own integration of its provider."
                    )
                if kb_ids:
                    result_parts.append(f"  • Knowledge bases: {len(kb_ids)}")
                if mcp_ids:
                    result_parts.append(f"  • MCP servers: {len(mcp_ids)}")
                return str(agent.id), result_parts

        except Exception as e:
            logger.error("Agent builder create_agent failed: %s", format_exception_message(e), exc_info=True)
            return "Failed to create agent. Please check your input and try again."

    # ── clone_agent ───────────────────────────────────────────────────

    async def _clone_agent(
        source_agent_id: str,
        name: str,
        persona: str = "",
        persona_append: str = "",
        description: str = "",
        add_integration_bindings: str = "",
        remove_integration_bindings: str = "",
        add_knowledge_base_ids: str = "",
        remove_knowledge_base_ids: str = "",
        add_builtin_tool_ids: str = "",
        remove_builtin_tool_ids: str = "",
        copy_memory: bool | str = False,
    ) -> str:
        """A personal copy of an agent, with add/remove overrides.

        The agent row is copied through ``AGENT_COPY_FIELDS``, the manifest the
        REST duplicate uses (model, memory, scoping, deny list, read-only
        bindings, handover...), and a binding the caller could not bind is left
        out exactly as the duplicate leaves it out. Assignments and sharing
        never travel, nor a reserved tag (``builtin``, ``org-fill:``); the
        persistent memory travels with *copy_memory*, under the memory API's
        read rule. The result is a diff against the source.
        """
        from fastapi import HTTPException
        from sqlalchemy import select

        from app.api.v1.binding_access import ids_matching, integration_binding_ids
        from app.api.v1.helpers import (
            generate_unique_agent_slug,
            get_user_group_ids,
            lower_budget_to_ceiling,
            user_visible_filter,
        )
        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.agent_copy import build_agent_copy_kwargs, split_reserved_agent_tags
        from app.models.general_assistant_config import clamp_max_iterations
        from app.models.integration import Integration
        from app.models.knowledge_base import KnowledgeBase
        from app.services.agent_bindings import (
            BINDING_MODELS,
            copy_warnings,
            retain_agent_bindings,
            retain_copier_channels,
            validate_personal_agent_bindings,
        )
        from app.services.agent_config_versioning import seed_initial_agent_versions
        from app.services.audit import log_audit
        from app.services.llm_provider_deletion import (
            ProviderLockBusyError,
            lock_provider_configs,
            provider_reference_problem,
        )
        from app.services.persistent_memory import (
            MEMORY_COPY_REFUSED,
            copy_agent_memories,
            memory_content_restricted,
        )

        uid = safe_uuid(user_id)
        if uid is None:
            return "Agent cloning is not available for anonymous users."
        source_uuid, id_err = _validate_id_arg(source_agent_id, "source_agent_id")
        if id_err:
            return id_err
        for label, value in (
            ("name", name),
            ("persona", persona),
            ("persona_append", persona_append),
            ("description", description),
            ("add_integration_bindings", add_integration_bindings),
            ("remove_integration_bindings", remove_integration_bindings),
            ("add_knowledge_base_ids", add_knowledge_base_ids),
            ("remove_knowledge_base_ids", remove_knowledge_base_ids),
            ("add_builtin_tool_ids", add_builtin_tool_ids),
            ("remove_builtin_tool_ids", remove_builtin_tool_ids),
        ):
            if value is not None and not isinstance(value, str):
                return f"{label} must be a string (got {type(value).__name__})."
        name = (name or "").strip()
        if not name:
            return "A name for the clone is required."
        if (persona or "").strip() and (persona_append or "").strip():
            return "Pass persona (replace) or persona_append (add to it), not both."
        copy_memory_value, copy_memory_err, _provided = _parse_bool_arg(copy_memory, "copy_memory")
        if copy_memory_err:
            return copy_memory_err
        want_memory = bool(copy_memory_value)

        add_pairs, bad_add = _parse_integration_pairs(add_integration_bindings)
        remove_pairs, bad_remove = _parse_integration_pairs(remove_integration_bindings)
        if bad_add or bad_remove:
            return (
                f"Invalid integration binding entry(ies): {', '.join(bad_add + bad_remove)}. "
                "Use 'provider:integration_id' pairs separated by commas."
            )
        add_kbs, bad_add_kbs = _parse_uuid_csv(add_knowledge_base_ids)
        remove_kbs, bad_remove_kbs = _parse_uuid_csv(remove_knowledge_base_ids)
        if bad_add_kbs or bad_remove_kbs:
            return (
                f"Invalid knowledge base id(s): {', '.join(bad_add_kbs + bad_remove_kbs)} "
                "- each must be a UUID."
            )
        add_tools = _csv_str_list(add_builtin_tool_ids)
        remove_tools = _csv_str_list(remove_builtin_tool_ids)
        unknown_tools = [t for t in add_tools if t not in _VALID_BUILTIN_TOOL_IDS]
        if unknown_tools:
            return (
                f"Invalid add_builtin_tool_ids: {', '.join(unknown_tools)}. "
                f"Supported: {', '.join(_advertised_builtin_tool_ids())}."
            )

        async with _builder_session() as _db:
            # The copy carries the source's provider pin: read it under the
            # provider config lock, as the REST duplicate does.
            try:
                await lock_provider_configs(_db, shared=True)
            except ProviderLockBusyError as exc:
                return exc.detail
            caller = await _load_caller_user_for_builder(_db, uid)
            if caller is None:
                return "User not found."
            # An administrator-only group (#11594) is added by an administrator alone.
            from app.services.builtin_tools.organization_context_write import refused_admin_bound_ids

            _copy_refused = refused_admin_bound_ids(add_tools, caller_is_admin=bool(caller.is_admin))
            if _copy_refused:
                return f"Only an administrator can bind these built-in tools: {', '.join(_copy_refused)}."
            admin_bypass = agent_tool_admin_bypass(caller)
            group_ids = await get_user_group_ids(_db, uid)
            source = (
                await _db.execute(
                    select(Agent).where(
                        Agent.id == source_uuid,
                        user_visible_filter(Agent, uid, group_ids, is_admin=admin_bypass),
                    )
                )
            ).scalar_one_or_none()
            if source is None:
                return f"Agent `{source_agent_id}` not found or you don't have access."
            if want_memory and memory_content_restricted(source, caller):
                return f"Agent not cloned: {MEMORY_COPY_REFUSED} Clone it without copy_memory."
            _kept_tags, dropped_tags = split_reserved_agent_tags(source.tags)

            copied_provider_id = source.llm_provider_id
            try:
                if copied_provider_id is not None and await provider_reference_problem(
                    _db,
                    copied_provider_id,
                    stored=None,
                    label="the copied model",
                    user_id=uid,
                    scoped=not admin_bypass,
                ):
                    copied_provider_id = None
            except ProviderLockBusyError as exc:
                return exc.detail

            clone = Agent(
                **build_agent_copy_kwargs(
                    source,
                    user_id=uid,
                    is_company_managed=False,
                    name=name,
                    slug=await generate_unique_agent_slug(_db, name),
                    llm_provider_id=copied_provider_id,
                    max_iterations=(
                        clamp_max_iterations(source.max_iterations)
                        if source.max_iterations is not None
                        else None
                    ),
                    budget_config=lower_budget_to_ceiling(source.budget_config, user=caller),
                    available_channel_types=(
                        list(source.available_channel_types)
                        if source.available_channel_types
                        else ["web"]
                    ),
                    web_enabled=False,
                )
            )

            # A binding of someone else's agent the caller could not bind
            # through create / update is left out, as the duplicate does.
            dropped: dict[str, int] = {}
            if source.user_id != uid and not admin_bypass:
                keep: dict[str, set[str]] = {}
                for field, model in BINDING_MODELS.items():
                    raw = getattr(clone, field)
                    ids = integration_binding_ids(raw) if field == "integration_bindings" else raw
                    keep[field] = await ids_matching(
                        _db, model, ids, user_visible_filter(model, uid, group_ids, is_admin=False)
                    )
                dropped = retain_agent_bindings(clone, keep)
                original_channels = clone.channel_config or {}
                kept_channels, dropped["channel_config"] = await retain_copier_channels(
                    _db, clone.channel_config, user=caller, user_group_ids=group_ids
                )
                if dropped["channel_config"]:
                    clone.channel_config = kept_channels or None
                    gone = set(original_channels) - set(kept_channels or {})
                    clone.available_channel_types = [
                        t for t in clone.available_channel_types or [] if t not in gone
                    ] or ["web"]
            copied_pairs = _binding_pairs(clone.integration_bindings)
            copied_kbs = [str(k) for k in clone.knowledge_base_ids or []]
            copied_tools = list(clone.builtin_tool_ids or [])

            # Overrides: removals first, so a pair both removed and added ends bound.
            bindings = {p: [str(i) for i in ids] for p, ids in (clone.integration_bindings or {}).items()}
            for provider, ids in remove_pairs.items():
                bindings[provider] = [i for i in bindings.get(provider, []) if i not in ids]
            for provider, ids in add_pairs.items():
                bound = bindings.setdefault(provider, [])
                bound.extend(i for i in ids if i not in bound)
            clone.integration_bindings = {p: ids for p, ids in bindings.items() if ids} or None
            kbs = [k for k in copied_kbs if k not in remove_kbs]
            kbs.extend(k for k in add_kbs if k not in kbs)
            clone.knowledge_base_ids = kbs or None
            tools = [t for t in copied_tools if t not in remove_tools]
            tools.extend(t for t in add_tools if t not in tools)
            if not caller.is_admin:
                # A non-administrator's copy never carries an administrator-only group (#11594).
                from app.services.builtin_tools.organization_context_write import ADMIN_BOUND_BUILTIN_TOOL_IDS

                tools = [t for t in tools if t not in ADMIN_BOUND_BUILTIN_TOOL_IDS]
            clone.builtin_tool_ids = tools
            if (persona or "").strip():
                clone.persona = persona.strip()
            elif (persona_append or "").strip():
                clone.persona = f"{(clone.persona or '').rstrip()}\n\n{persona_append.strip()}".strip()
            if (description or "").strip():
                clone.description = description.strip()
            clone.prune_orphaned_integration_scopes()
            clone.prune_orphaned_knowledge_base_permissions()

            # An added resource must be one the caller may bind, as on create.
            added_pairs = sorted(_binding_pairs(clone.integration_bindings) - copied_pairs)
            added_kbs = [k for k in kbs if k not in copied_kbs]
            if added_pairs or added_kbs:
                added_bindings: dict[str, list[str]] = {}
                for provider, iid in added_pairs:
                    added_bindings.setdefault(provider, []).append(iid)
                try:
                    await validate_personal_agent_bindings(
                        _db,
                        uid,
                        group_ids,
                        is_admin=admin_bypass,
                        mcp_server_ids=None,
                        knowledge_base_ids=added_kbs or None,
                        integration_bindings=added_bindings or None,
                        handover_agent_ids=None,
                        custom_tool_ids=None,
                        skill_ids=None,
                        prompt_ids=None,
                    )
                except HTTPException as exc:
                    return f"Agent not cloned: {exc.detail}"

            _db.add(clone)
            await _db.flush()
            await seed_initial_agent_versions(_db, agent=clone, author_id=uid)
            memory_copied = (
                await copy_agent_memories(_db, source=source, target_agent_id=clone.id, user=caller)
                if want_memory
                else 0
            )
            await log_audit(
                _db,
                user=caller,
                action="create",
                entity_type="agent",
                entity_id=clone.id,
                entity_name=clone.name,
                summary=f"Cloned agent '{source.name}' as '{clone.name}'",
                details={
                    "source_agent_id": str(source.id),
                    "slug": clone.slug,
                    "bindings_not_copied": {k: v for k, v in dropped.items() if v},
                    "reserved_tags_dropped": dropped_tags,
                    "memory_entries_copied": memory_copied,
                    "via": "agent",
                },
            )

            removed_pairs = sorted(copied_pairs - _binding_pairs(clone.integration_bindings))
            removed_kbs = [k for k in copied_kbs if k not in kbs]
            named_ids = [
                u for _, i in added_pairs + removed_pairs if (u := safe_uuid(i))
            ]
            integration_names = {
                str(row[0]): row[1]
                for row in (
                    await _db.execute(
                        select(Integration.id, Integration.name).where(
                            Integration.id.in_(named_ids),
                            user_visible_filter(Integration, uid, group_ids, is_admin=admin_bypass),
                        )
                    )
                ).all()
            } if named_ids else {}
            kb_lookup = [u for k in added_kbs + removed_kbs if (u := safe_uuid(k))]
            kb_names = {
                str(row[0]): row[1]
                for row in (
                    await _db.execute(
                        select(KnowledgeBase.id, KnowledgeBase.name).where(
                            KnowledgeBase.id.in_(kb_lookup),
                            user_visible_filter(KnowledgeBase, uid, group_ids, is_admin=admin_bypass),
                        )
                    )
                ).all()
            } if kb_lookup else {}
            clone_id, clone_slug = str(clone.id), clone.slug
            source_name = source.name
            source_persona = source.persona or ""
            clone_persona = clone.persona or ""
            provider_pin_dropped = source.llm_provider_id is not None and copied_provider_id is None
            not_requested_removals = [
                f"{p}:{i}" for p, ids in remove_pairs.items() for i in ids if (p, i) not in copied_pairs
            ] + [k for k in remove_kbs if k not in copied_kbs] + [
                t for t in remove_tools if t not in copied_tools
            ]
            try:
                await _db.commit()
            except Exception:
                logger.exception("clone_agent commit failed for source %s", source_agent_id)
                await _db.rollback()
                return f"Failed to clone agent `{source_agent_id}`. Please try again."

        def _named(label: str, names: dict[str, str], key: str) -> str:
            return f"{label} ({names[key]})" if key in names else label

        diff = [f"  • name: '{source_name}' -> '{name}'"]
        if clone_persona != source_persona:
            how = "replaced" if (persona or "").strip() else "appended to"
            diff.append(
                f"  • persona: {how} ({len(source_persona)} -> {len(clone_persona)} characters)"
            )
        for provider, iid in added_pairs:
            diff.append(f"  • + integration {_named(f'{provider}:{iid}', integration_names, iid)}")
        for provider, iid in removed_pairs:
            diff.append(f"  • - integration {_named(f'{provider}:{iid}', integration_names, iid)}")
        for kb in added_kbs:
            diff.append(f"  • + knowledge base {_named(kb, kb_names, kb)}")
        for kb in removed_kbs:
            diff.append(f"  • - knowledge base {_named(kb, kb_names, kb)}")
        for tool in tools:
            if tool not in copied_tools:
                diff.append(f"  • + built-in tool {tool}")
        for tool in copied_tools:
            if tool not in tools:
                diff.append(f"  • - built-in tool {tool}")
        if provider_pin_dropped:
            diff.append("  • model provider pin not copied: you may not use that provider")
        diff.extend(f"  • {warning}" for warning in copy_warnings(dropped))
        if not_requested_removals:
            diff.append(
                "  • not on the source, nothing removed: " + ", ".join(not_requested_removals)
            )
        if dropped_tags:
            diff.append(
                "  • reserved tags not copied (the clone is not the platform's agent): "
                + ", ".join(dropped_tags)
            )
        if want_memory:
            diff.append(
                f"  • memory: {memory_copied} persistent memory entr{'y' if memory_copied == 1 else 'ies'} "
                "copied (dates, types, subjects, user scopes and superseded state kept)"
            )

        # Intent bindings never travel with a clone: the clone is personal, and
        # a global binding needs a company-managed or group-shared agent (the
        # gate of ``PUT /intents/{id}/bind``). Each one is named so the caller
        # does not take the clone for an interchangeable copy in a connected
        # platform's AI component.
        from .intent_bindings import BIND_TOOL
        from .intent_bindings import source_intent_bindings as _source_intent_bindings

        try:
            async with _builder_session() as _intent_db:
                _not_carried = await _source_intent_bindings(_intent_db, source_uuid) if source_uuid is not None else []
        except Exception:
            # The clone is committed: a failed read only loses the note.
            logger.exception("clone_agent could not read the intent bindings of %s", source_agent_id)
            _not_carried = []
            diff.append("  • intent bindings of the source not read: check them with list_agent_intent_bindings")
        for intent_name, vertical, builtin in _not_carried:
            why = (
                "a built-in default binding"
                if builtin
                else (
                    "the clone is private; share it with a group or make it company-managed, "
                    f"then bind it with {BIND_TOOL}"
                )
            )
            diff.append(f"  • intent binding not copied: {intent_name} ({vertical or 'all'}) - {why}")

        _slug, binding_lines = await _describe_agent_bindings(clone_id)
        return "\n".join(
            [
                f"Agent **{name}** cloned from **{source_name}**.",
                f"  • ID: `{clone_id}`",
                f"  • Slug: `{clone_slug}`",
                *binding_lines,
                "",
                "Differences from the source:",
                *diff,
                "",
                "Everything else is the source's (model, temperature, reasoning effort, "
                "max iterations, memory flags, scoping, deny list, read-only bindings, "
                "handover). Not copied: assignments, "
                + ("" if want_memory else "memory (copy_memory copies it), ")
                + "sharing, web exposure, intent bindings, reserved tags.",
            ]
        )

    # ── run_agent_once ────────────────────────────────────────────────

    async def _run_agent_once(
        agent_id: str = "",
        prompt: str = "",
        task_id: str = "",
        file_ids: list[str] | str | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        """Run an agent once on a prompt and hand back its answer.

        A smoke test that leaves no assignment behind: the run is a durable
        background task of the agent itself (the sub-agent machinery a
        ``spawn_background_task`` uses), awaited for up to
        :data:`_RUN_ONCE_WAIT_SECONDS`. A run still going by then is collected
        by calling again with its ``task_id``. The task gets a run of its own
        (trigger ``run_once``, the caller's run as parent when there is one),
        so it is listed with the agent's runs and has that run's sandbox
        workspace: a file code_interpreter writes under ``workspace/`` stays
        for the next call and a ``$workspace:`` reference resolves.

        ``file_ids`` ride on the run's message as a chat message's
        attachments do (images as vision blocks): files of the caller's turn or
        rows the caller owns, at most ``MAX_TURN_FILES``.
        """
        from app.core.database import async_session as _builder_session
        from app.services.sub_agent import (
            RUN_ONCE_TRIGGER_TYPE,
            create_sub_agent_task,
            get_task_result,
            wait_for_tasks,
        )
        from app.services.sub_agent_files import MAX_TURN_FILES, TURN_FILE_IDS_KEY

        uid = safe_uuid(user_id)
        if uid is None:
            return "Running an agent is not available for anonymous users."
        if _arg_provided(task_id):
            task_uuid, id_err = _validate_id_arg(task_id, "task_id")
            if id_err:
                return id_err
            tid = str(task_uuid)
        else:
            agent_uuid, id_err = _validate_id_arg(agent_id, "agent_id")
            if id_err:
                return id_err
            if not isinstance(prompt, str) or not prompt.strip():
                return "prompt is required: what the agent should do in this run."
            async with _builder_session() as _db:
                agent, perm_err = await _load_writable_agent_for_builder(
                    _db, uid, agent_uuid, action="run"
                )
                if perm_err is not None or agent is None:
                    return perm_err or f"Agent `{agent_id}` not found or you don't have access."
                if agent.enabled is False:
                    return f"Agent **{agent.name}** is disabled. Enable it first."
            wanted = [file_ids] if isinstance(file_ids, str) else list(file_ids or [])
            wanted = [str(raw).strip() for raw in wanted if str(raw or "").strip()]
            extra_metadata: dict = {"origin": "run_agent_once"}
            task_files = None
            if wanted:
                if len(wanted) > MAX_TURN_FILES:
                    return f"At most {MAX_TURN_FILES} files ride on one message ({len(wanted)} given). Nothing was run."
                from app.services.work_item_files import resolve_attachable_files
                from app.tools.base import FileContext as _FileContext

                attachable = await resolve_attachable_files(
                    wanted,
                    file_context=file_context,
                    conversation_id=None,
                    assignment_run_id=None,
                    user_id=uid,
                )
                if attachable.refused:
                    return (
                        "These files cannot be attached (not found, or not yours): "
                        f"{', '.join(attachable.refused)}. Nothing was run."
                    )
                task_files = _FileContext.from_file_refs(attachable.refs)
                extra_metadata[TURN_FILE_IDS_KEY] = [str(ref.file_id) for ref in attachable.refs]
            try:
                task = await create_sub_agent_task(
                    None,
                    user_id=str(uid),
                    parent_agent_id=str(agent_uuid),
                    target_agent_id=str(agent_uuid),
                    task_description=prompt.strip(),
                    allow_self=True,
                    extra_metadata=extra_metadata,
                    own_run_trigger=RUN_ONCE_TRIGGER_TYPE,
                    parent_run_id=str(caller_run) if (caller_run := safe_uuid(run_id)) else None,
                    **({"file_context": task_files} if task_files is not None else {}),
                )
            except Exception as exc:
                logger.warning("run_agent_once failed to start: %s", format_exception_message(exc))
                return f"Failed to start the run: {format_exception_message(exc)}"
            if task is None:
                return "Failed to start the run. Please try again."
            tid = str(task.id)

        await wait_for_tasks([tid], str(uid), budget=_RUN_ONCE_WAIT_SECONDS)
        result = await get_task_result(None, tid, str(uid))
        if result is None:
            return f"Run `{tid}` not found."
        status = result.get("status")
        # The run id get_run reads, beside the task id this tool collects with.
        run_ref = result.get("run_id")
        label = f"Run `{run_ref}` (task `{tid}`)" if run_ref and str(run_ref) != tid else f"Run `{tid}`"
        if status == "completed":
            return f"{label} completed. The agent answered:\n\n{result.get('result') or '(no output)'}"
        if status in ("failed", "cancelled", "incomplete"):
            return f"{label} {status}: {result.get('error') or 'no error recorded'}"
        return f"{label} is still {status}. Call run_agent_once(task_id='{tid}') to keep waiting for its answer."

    # ── list_integration_tools ────────────────────────────────────────

    async def _list_integration_tools(provider: str = "", integration_id: str = "") -> str:
        """Each tool of a provider with its access: the classification a
        read-only binding enforces. A provider whose every integration is its
        own API lists the operations of each integration's imported spec.
        """
        from sqlalchemy import select

        from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_tools_for_provider
        from app.core.database import async_session as _builder_session
        from app.models.custom_integration_definition import CustomIntegrationDefinition
        from app.services.integration_read_only import integration_tool_access
        from app.services.integration_tools import custom_declared_tool_names
        from app.services.openapi_import.service import (
            PER_USER_UNRESOLVABLE_PROVIDERS as _SPEC_PROVIDERS,
        )

        key = (provider or "").strip() if isinstance(provider, str) else ""
        raw_id = (integration_id or "").strip() if isinstance(integration_id, str) else ""
        if raw_id:
            parsed_id = safe_uuid(raw_id)
            if parsed_id is None:
                return f"integration_id '{raw_id}' is not a valid UUID."
            rows = await _load_imported_tool_listing(user_id, integration_id=parsed_id)
            if not rows:
                return f"Integration `{raw_id}` not found, or you cannot see it."
            if rows[0][2] not in _SPEC_PROVIDERS:
                # A catalog provider: its tools are the provider catalog's.
                key = rows[0][2]
            else:
                listing = _format_imported_tool_listing(rows)
                if listing is None:
                    return (
                        f"**{rows[0][1]}** (`{raw_id}`) has no tools: no spec is "
                        "imported (update_integration attaches one)."
                    )
                return listing + "\n" + _READ_ONLY_TOOL_NOTE
        if not key:
            return (
                "provider (or integration_id) is required: a provider key of the "
                "integration catalog (list_integration_catalog lists them)."
            )
        if key.lower() in _SPEC_PROVIDERS:
            key = key.lower()
            rows = await _load_imported_tool_listing(user_id, provider=key)
            listing = _format_imported_tool_listing(rows)
            label = PROVIDER_CATALOG.get(key, {}).get("name", key)
            if listing is None:
                return (
                    f"{label} (`{key}`) has no tools: none of its integrations "
                    "you can see has an imported spec."
                )
            return (
                f"{label} (`{key}`): each integration's tools come from "
                f"its own imported spec.\n\n{listing}\n{_READ_ONLY_TOOL_NOTE}"
            )
        if key.lower().startswith("custom:"):
            async with _builder_session() as _db:
                row = (
                    await _db.execute(
                        select(CustomIntegrationDefinition).where(
                            CustomIntegrationDefinition.provider_key == key
                        )
                    )
                ).scalar_one_or_none()
            if row is None:
                return f"Unknown custom provider '{key}'."
            label, names = row.name, custom_declared_tool_names(row.metadata_json)
            access = {name: integration_tool_access(name) for name in names}
        else:
            key = key.lower()
            tools = build_tools_for_provider(key)
            if tools is None:
                return (
                    f"Unknown provider '{key}', or it has no tools. Use "
                    "list_integration_catalog to find the provider key."
                )
            label = PROVIDER_CATALOG.get(key, {}).get("name", key)
            names = [tool.name for tool in tools]
            # The catalog's type honours a definition's read_only declaration
            # (definition_tool_type), as the badge and a read-only binding do.
            access = {tool.name: "read" if tool.tool_type == "read" else "write" for tool in tools}
        if not names:
            return f"{label} (`{key}`) has no tools."
        reads = sum(1 for value in access.values() if value == "read")
        lines = [
            f"**{label}** (`{key}`) - {len(names)} tools "
            f"({reads} read, {len(names) - reads} write):"
        ]
        lines.extend(f"  - {name} [{access[name]}]" for name in names)
        lines.append(_READ_ONLY_TOOL_NOTE)
        lines.append(
            "An agent binding several integrations of this provider gets each tool "
            "with the integration's tool suffix (e.g. `get_profile__corp`): "
            "list_integrations shows each suffix, which a rename never changes; an "
            "assignment gate may name the base tool plus the integration id instead."
        )
        return "\n".join(lines)

    # ── add_assignment_to_agent ─────────────────────────────────────────

    async def _add_assignment_to_agent(
        agent_id: str,
        name: str,
        prompt: str,
        activation_prompt: str = "",
        trigger_type: str = "manual",
        trigger_config: str = "",
        description: str = "",
        delay: str = "",
        human_in_the_loop: bool | None = None,
        human_in_the_loop_prompt: str = "",
        ai_label: bool = True,
        ai_label_prompt: str = "",
        notify_on_complete: bool = False,
        show_in_digest: bool = False,
        dry_run: bool = False,
        ignore_self_triggered_events: bool = False,
        retry_on_failure: bool = False,
        max_retries: int = 3,
        max_iterations: int | None = None,
        timeout_seconds: int = 300,
        enabled: bool = True,
        activation_check: str | dict | None = "",
        wait_policy: object = "",
        organization_context_mode: str = "",
        organization_context_categories: object = None,
    ) -> str:
        from app.models.assignment import Assignment, assignment_text_length_error
        from app.services.assignment_activation_check import parse_activation_check

        try:
            try:
                uid = uuid.UUID(user_id)
            except (ValueError, AttributeError):
                return "Assignment creation is not available for anonymous users."
            check_value, check_err, _check_provided = parse_activation_check(activation_check)
            if check_err is not None:
                return check_err

            agent_uuid, id_err = _validate_id_arg(agent_id, "agent_id")
            if id_err:
                return id_err

            # Type-guard the string fields before any .strip()/.lower(): tool
            # kwargs are forwarded verbatim, so a native JSON non-string (e.g.
            # true / 1) would otherwise raise AttributeError and surface only the
            # generic "Failed to add assignment". None is allowed (optional
            # fields treat it as empty).
            for _str_label, _str_value in (
                ("name", name),
                ("prompt", prompt),
                ("activation_prompt", activation_prompt),
                ("trigger_type", trigger_type),
                ("trigger_config", trigger_config),
                ("description", description),
                ("delay", delay),
                ("human_in_the_loop_prompt", human_in_the_loop_prompt),
                ("ai_label_prompt", ai_label_prompt),
            ):
                if _str_value is not None and not isinstance(_str_value, str):
                    return f"{_str_label} must be a string."
                _surrogate_err = lone_surrogate_error(_str_label, _str_value)
                if _surrogate_err is not None:
                    return _surrogate_err

            name = (name or "").strip()
            if not name:
                return (
                    "Assignment name is required (whitespace-only names are not "
                    "allowed)."
                )
            # Measured as stored, stripped. ``trigger_type`` needs no check: the
            # allow-list below admits only types shorter than its column.
            for _len_field, _len_value in (("name", name), ("delay", (delay or "").strip())):
                _len_err = assignment_text_length_error(_len_field, _len_value)
                if _len_err is not None:
                    return _len_err

            # Use a dedicated session to avoid "concurrent operations" errors
            # when the LLM calls multiple add_assignment tools in parallel.
            from app.core.database import async_session as _builder_session

            async with _builder_session() as _db:
                # Enforce the same write gates as the REST API: built-in
                # agents are read-only, company-managed agents require
                # admin, group-shared agents require ``config`` tier.
                # Without this gate the tool silently bypassed everything
                # the UI enforced.
                agent, perm_err = await _load_writable_agent_for_builder(
                    _db,
                    uid,
                    agent_uuid,
                    action="add an assignment to",
                    assignment_write=True,
                )
                if check_value is not None:
                    from app.services.fleets import gate_fleet_refusal

                    _gate_refusal = await gate_fleet_refusal(_db, check_value)
                    if _gate_refusal is not None:
                        return f"{_gate_refusal} Nothing was saved."
                if perm_err is not None or agent is None:
                    return perm_err or (
                        f"Agent `{agent_id}` not found or you don't have access to it."
                    )

                trig_config: dict = {}
                if trigger_config:
                    parsed_config, config_err = _parse_trigger_config_arg(
                        trigger_config
                    )
                    if config_err is not None:
                        return config_err
                    trig_config = parsed_config

                if trigger_type not in ASSIGNMENT_TRIGGER_TYPES:
                    return (
                        "Invalid trigger_type: must be one of "
                        f"{', '.join(ASSIGNMENT_TRIGGER_TYPES)}"
                    )
                work_item_err = work_item_trigger_error(trigger_type, trig_config)
                if work_item_err is not None:
                    return work_item_err
                platform_event_err = platform_event_trigger_error(trigger_type, trig_config)
                if platform_event_err is not None:
                    return platform_event_err
                from app.services.platform_events import fleet_scope_error

                fleet_err = await fleet_scope_error(trigger_type, trig_config, agent.id)
                if fleet_err is not None:
                    return fleet_err
                from app.services.trigger_provisioning import provision_spec_error

                provision_err = provision_spec_error(trigger_type, trig_config)
                if provision_err is not None:
                    return f"{provision_err} Nothing was saved."

                # Reject invalid cron expressions on schedule triggers, like the
                # API schema does for HTTP-driven creates.
                schedule_err = schedule_trigger_error(trigger_type, trig_config)
                if schedule_err is not None:
                    return schedule_err
                # The integration the trigger reads events from must be one the
                # caller can see, like the REST create.
                trigger_err = await _trigger_integration_error(_db, uid, trig_config)
                if trigger_err is not None:
                    return trigger_err

                # Guard the safety/limit integers so the LLM can't persist a
                # nonsensical value (e.g. a negative timeout or a runaway
                # iteration cap). Bounds mirror ``update_assignment``.
                for _int_label, _int_value, _int_min, _int_max in (
                    ("timeout_seconds", timeout_seconds, 0, 3600),
                    ("max_retries", max_retries, 0, 10),
                ):
                    if not isinstance(_int_value, int) or isinstance(_int_value, bool):
                        return f"{_int_label} must be an integer."
                    if _int_value < _int_min or _int_value > _int_max:
                        return (
                            f"{_int_label} must be between {_int_min} and "
                            f"{_int_max}."
                        )

                # max_iterations is inherit-able for assignments: None = inherit
                # (agent override -> platform default). A real value OVERRIDES
                # the agent and is clamped to the shared ceiling.
                from app.models.general_assistant_config import (
                    MAX_ITERATIONS_CEILING,
                    clamp_max_iterations,
                )

                if max_iterations is not None:
                    if not isinstance(max_iterations, int) or isinstance(
                        max_iterations, bool
                    ):
                        return (
                            "max_iterations must be an integer (or omitted to "
                            "inherit from the agent / platform default)."
                        )
                    if max_iterations < 1 or max_iterations > MAX_ITERATIONS_CEILING:
                        return (
                            f"max_iterations must be between 1 and "
                            f"{MAX_ITERATIONS_CEILING}."
                        )
                    max_iterations = clamp_max_iterations(max_iterations)

                # wait_policy: narrows the agent's / platform's wait
                # policy for this assignment's runs.
                _wp_val, _wp_err, _ = _parse_wait_policy_arg(wait_policy)
                if _wp_err:
                    return _wp_err
                # organization_context_mode: inherit (blank) / always / off (#11533).
                _org_mode = "inherit"
                if isinstance(organization_context_mode, str) and organization_context_mode.strip():
                    from app.services.organization_context_injection import check_mode

                    try:
                        _org_mode = check_mode(organization_context_mode)
                    except ValueError as exc:
                        return f"Invalid {exc}."
                # organization_context_categories: omitted / "inherit" = inherit (#11533).
                from app.services.organization_context_categories import check_categories

                try:
                    _org_categories = check_categories(organization_context_categories)
                except ValueError as exc:
                    return f"Invalid {exc}."

                # Guard the boolean execution settings too: tool-call kwargs are
                # forwarded verbatim, so a stringified "true"/"false" (truthy in
                # Python) would otherwise be persisted as the wrong value or
                # rejected by the ORM. ``human_in_the_loop`` is tri-state
                # (None = inherit from the agent); the rest must be real bools.
                if human_in_the_loop is not None and not isinstance(
                    human_in_the_loop, bool
                ):
                    return (
                        "human_in_the_loop must be true, false, or null "
                        "(inherit from the agent)."
                    )
                for _bool_label, _bool_value in (
                    ("enabled", enabled),
                    ("ai_label", ai_label),
                    ("notify_on_complete", notify_on_complete),
                    ("show_in_digest", show_in_digest),
                    ("dry_run", dry_run),
                    ("ignore_self_triggered_events", ignore_self_triggered_events),
                    ("retry_on_failure", retry_on_failure),
                ):
                    if not isinstance(_bool_value, bool):
                        return f"{_bool_label} must be true or false."

                # Webhook assignments are born with a fresh secret like
                # API-created ones (SEC-5).
                from app.core.utils import ensure_webhook_secret

                wf = Assignment(
                    # Mirror the API's ownership rule (``create_assignment``):
                    # the assignment is always owned by the agent's owner — NOT
                    # the caller — so it stays visible to whoever owns the
                    # agent. ``is_company_managed`` mirrors the agent and drives
                    # visibility; the owner (``user_id``) is always preserved.
                    # The write-gate above (``_load_writable_agent_for_builder``)
                    # prevents non-admin non-group-shared callers from touching
                    # a colleague's private agent, so handing ownership to
                    # ``agent.user_id`` cannot be abused.
                    user_id=agent.user_id,
                    is_company_managed=agent.is_company_managed,
                    agent_id=agent.id,
                    name=name,
                    description=(description or "").strip() or None,
                    trigger_type=trigger_type,
                    trigger_config=ensure_webhook_secret(
                        trigger_type, new_trigger_config(trigger_type, trig_config)
                    ),
                    prompt=prompt,
                    # Optional activation gate ("should I run?") - distinct from
                    # the action ``prompt``. Empty/whitespace or the literal
                    # "none" means no gate (always runs); any real value is
                    # stored as-is so prompt-body formatting is preserved,
                    # matching ``update_assignment`` (which never trims the
                    # stored bytes) and the action ``prompt`` above.
                    activation_prompt=(
                        None
                        if not (activation_prompt or "").strip()
                        or activation_prompt.strip().lower() == "none"
                        else activation_prompt
                    ),
                    # The tool-capable gate, evaluated before the prompt gate.
                    activation_check=check_value,
                    # Normalise delay like the other free-text fields: strip,
                    # and treat blank / the literal "none" as NULL (no delay) so
                    # a padded value or " none " can't persist confusing state.
                    delay=(
                        None
                        if not (delay or "").strip()
                        or delay.strip().lower() == "none"
                        else delay.strip()
                    ),
                    human_in_the_loop=human_in_the_loop,
                    # Optional per-assignment prompt overrides: blank or the
                    # literal "none" means "use the platform default" (stored as
                    # NULL); a real value is kept verbatim so prompt-body
                    # formatting survives, matching ``update_assignment``.
                    human_in_the_loop_prompt=(
                        None
                        if not (human_in_the_loop_prompt or "").strip()
                        or human_in_the_loop_prompt.strip().lower() == "none"
                        else human_in_the_loop_prompt
                    ),
                    ai_label=ai_label,
                    ai_label_prompt=(
                        None
                        if not (ai_label_prompt or "").strip()
                        or ai_label_prompt.strip().lower() == "none"
                        else ai_label_prompt
                    ),
                    notify_on_complete=notify_on_complete,
                    show_in_digest=show_in_digest,
                    dry_run=dry_run,
                    ignore_self_triggered_events=ignore_self_triggered_events,
                    retry_on_failure=retry_on_failure,
                    max_retries=max_retries,
                    max_iterations=max_iterations,
                    wait_policy=_wp_val,
                    organization_context_mode=_org_mode,
                    organization_context_categories=_org_categories,
                    timeout_seconds=timeout_seconds,
                    enabled=enabled,
                )
                _db.add(wf)
                # A work_item inbox needs its queue tools: the ``work_items``
                # built-in is switched on with it, like the REST create.
                if trigger_type == "work_item":
                    _bt_ids = list(getattr(agent, "builtin_tool_ids", None) or [])
                    if "work_items" not in _bt_ids:
                        agent.builtin_tool_ids = [*_bt_ids, "work_items"]
                await _db.commit()

                # Tri-state HITL: None = inherit from the agent (matches the
                # persisted column), so don't render it as a plain "No".
                hitl_display = (
                    "Inherit from agent"
                    if human_in_the_loop is None
                    else ("Yes" if human_in_the_loop else "No")
                )
                return (
                    f"Assignment **{name}** added to agent **{agent.name}** successfully!\n"
                    f"  • ID: `{wf.id}`\n"
                    f"  • Trigger: {trigger_type}\n"
                    f"  • Human-in-the-loop: {hitl_display}"
                    + (f"\n  • Gate check: {_describe_gate_check(check_value)}" if check_value else "")
                )

        except Exception as e:
            logger.error("Agent builder add_assignment failed: %s", format_exception_message(e), exc_info=True)
            return "Failed to add assignment. Please check your input and try again."

    # ── get_agent ─────────────────────────────────────────────────────

    async def _get_agent(agent_id: str, sections: list[str] | str | None = None) -> str:
        from sqlalchemy import func, select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.core.utils import safe_uuid
        from app.models.agent import Agent
        from app.models.agent_skill import AgentSkill
        from app.models.assignment import Assignment
        from app.models.custom_tool import CustomTool
        from app.models.integration import Integration
        from app.models.knowledge_base import KnowledgeBase
        from app.models.mcp_server import MCPServer
        from app.models.prompt import Prompt

        agent_uuid, id_err = await _id_arg(agent_id, "agent_id")
        if id_err:
            return id_err
        wanted_sections, sections_err = parse_get_agent_sections(sections)
        if sections_err:
            return sections_err

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []

            q = select(Agent).where(
                Agent.id == agent_uuid,
                caller_visible_filter(Agent, uid, group_ids),
            )

            result = await _db.execute(q)
            agent = result.scalar_one_or_none()
            if not agent and uid:
                # Mirror the REST ``GET /agents/{id}`` visibility contract:
                # an admin can read ANY agent (``is_admin`` bypass) — without
                # this, an admin who found an agent via
                # ``list_agents(scope='all')`` could not inspect it. Checked
                # lazily so the common visible-agent path costs no extra
                # queries.
                caller = await _load_caller_user_for_builder(_db, uid)
                if agent_tool_admin_bypass(caller):
                    result = await _db.execute(
                        select(Agent).where(Agent.id == agent_uuid)
                    )
                    agent = result.scalar_one_or_none()
            if not agent:
                return f"Agent `{agent_id}` not found or you don't have access."

            wf_q = (
                select(func.count())
                .select_from(Assignment)
                .where(Assignment.agent_id == agent.id)
            )
            wf_count = (await _db.execute(wf_q)).scalar() or 0

            # Resolve attached prompts, skills, and handover targets to their
            # names so the agent can VERIFY (and faithfully re-edit) what is
            # attached — not just a count. get_agent previously omitted prompts,
            # skills, and handover routing entirely, forcing the agent to rely
            # on its own build record (it could never read these back).
            # Resolve bound-resource NAMES only for resources the caller can
            # actually see (own + group-shared + company-managed; the
            # company-wide tier alone for anonymous). A visible agent row can legitimately
            # reference a resource the caller cannot see — a stale group-share,
            # a legacy binding, or a company-managed agent bound to a private
            # resource — and resolving that id to a name would leak the name.
            # The id itself stays visible (it is already in the agent row the
            # caller can read); only the resolved name is gated, mirroring the
            # REST personal-binding visibility contract.
            def _visible_clause(model):
                return caller_visible_filter(model, uid, group_ids)

            async def _id_name_map(model, ids: list[str]) -> dict[str, str]:
                valid = [u for i in ids if (u := safe_uuid(i))]
                if not valid:
                    return {}
                rows = await _db.execute(
                    select(model.id, model.name).where(
                        model.id.in_(valid), _visible_clause(model)
                    )
                )
                return {str(r[0]): r[1] for r in rows.all()}

            prompt_ids = [str(p) for p in (agent.prompt_ids or []) if p]
            skill_ids = [str(s) for s in (agent.skill_ids or []) if s]
            handover_ids = [str(h) for h in (agent.handover_agent_ids or []) if h]
            handover_types = agent.handover_types or {}
            handover_conditions = getattr(agent, "handover_conditions", None) or {}

            prompt_names = await _id_name_map(Prompt, prompt_ids)
            skill_names = await _id_name_map(AgentSkill, skill_ids)
            handover_names = await _id_name_map(Agent, handover_ids)

            # Resolve bound knowledge bases, MCP servers, and custom tools to
            # their names+ids too (same treatment as prompts/skills/handovers):
            # the agent must be able to read back the EXACT bound resources so
            # it can add or remove a single one (via update_agent binding_mode)
            # without re-deriving the whole set from the persona.
            kb_id_list = [str(k) for k in (agent.knowledge_base_ids or []) if k]
            mcp_id_list = [str(m) for m in (agent.mcp_server_ids or []) if m]
            ct_id_list = [str(c) for c in (agent.custom_tool_ids or []) if c]
            kb_names = await _id_name_map(KnowledgeBase, kb_id_list)
            mcp_names = await _id_name_map(MCPServer, mcp_id_list)
            ct_names = await _id_name_map(CustomTool, ct_id_list)

            # Integration bindings are stored as {provider: [integration_id]}.
            # Surface them as copy-pasteable 'provider:id' pairs WITH the
            # integration name so the agent can feed them straight back into
            # update_agent — previously only a COUNT was shown, which forced the
            # agent to reconstruct bindings from its persona and risk dropping
            # or swapping them when it had to re-send the whole set.
            raw_bindings = agent.integration_bindings or {}
            flat_integ_ids = [
                u
                for ids in raw_bindings.values()
                for i in (ids or [])
                if (u := safe_uuid(i))
            ]
            # Same visibility gate as the other name lookups (see
            # ``_visible_clause``): never reveal the NAME of an integration the
            # caller can't see, even when a visible agent row references its id.
            integ_names: dict[str, str] = {}
            # The tool-name suffix of each visible binding, read with
            # its name so update_agent callers see what the tools are called.
            integ_slugs: dict[str, str] = {}
            if flat_integ_ids:
                integ_rows = await _db.execute(
                    select(Integration.id, Integration.name, Integration.tool_slug).where(
                        Integration.id.in_(flat_integ_ids),
                        _visible_clause(Integration),
                    )
                )
                for r in integ_rows.all():
                    integ_names[str(r[0])] = r[1]
                    if len(r) > 2 and r[2]:
                        integ_slugs[str(r[0])] = r[2]
            # Bindings whose integration the agent's owner can no longer see:
            # its runs refuse their tools (drop_orphaned_bindings).
            _orphaned_ids: set[str] = set()
            if flat_integ_ids and not getattr(agent, "is_company_managed", False):
                from app.services.integration_credentials import orphaned_binding_ids

                _orphaned_ids = await orphaned_binding_ids(_db, getattr(agent, "user_id", None), flat_integ_ids)

            def _fmt_named(ids: list[str], names: dict[str, str]) -> str:
                if not ids:
                    return "None"
                items = [
                    (f"{names[i]} (`{i}`)" if i in names else f"`{i}`") for i in ids
                ]
                return f"{len(ids)} — " + ", ".join(items)

            def _fmt_handovers() -> str:
                if not handover_ids:
                    return "None"
                items = []
                for hid in handover_ids:
                    # Mirrors the runtime handover resolution: a target with no
                    # entry runs as "delegate", while an empty map is the legacy
                    # mode that gives every target every handover type.
                    htype = (
                        "all types"
                        if is_legacy_handover_map(handover_types)
                        else handover_types.get(hid, "delegate")
                    )
                    label = f"{handover_names[hid]} (`{hid}`)" if hid in handover_names else f"`{hid}`"
                    cond = handover_conditions.get(hid)
                    suffix = f' [{htype}] if "{cond}"' if cond else f" [{htype}]"
                    items.append(f"{label}{suffix}")
                return f"{len(handover_ids)} — " + ", ".join(items)

            from app.services.email_signature_settings import override_label, stored_overrides
            from app.services.integration_read_only import read_only_integration_ids

            _read_only_ids = read_only_integration_ids(agent)
            _signature_overrides = stored_overrides(getattr(agent, "integration_signatures", None))
            from app.services.integration_live_writes import (
                override_label as live_writes_label,
            )
            from app.services.integration_live_writes import (
                stored_live_writes,
            )

            _live_overrides = stored_live_writes(getattr(agent, "integration_live_writes", None))
            from app.services.tool_scope_names import unknown_integration_tool_names

            # A stored list is never refused on read; a name its provider no
            # longer exposes is flagged so a sweep can clean it.
            _stale_scope_names = await unknown_integration_tool_names(
                _db, getattr(agent, "integration_allowed_tools", None)
            )

            def _fmt_outside_builtin_allowlist() -> str:
                # A function shipped in a scoped group after the list was
                # written is never granted silently, nor hidden (#12192).
                from app.services.builtin_allowed_tools import builtin_functions_outside_allowlist

                outside = builtin_functions_outside_allowlist(_g("builtin_allowed_tools", None))
                if not outside:
                    return "None"
                return "; ".join(f"{group}: {', '.join(names)}" for group, names in outside.items())

            def _fmt_allowlist_not_enabled() -> str:
                # An entry for a group the agent does not enable grants
                # nothing: flagged, never shown as a grant (#1428).
                from app.services.builtin_allowed_tools import allowlisted_groups_not_enabled

                groups = allowlisted_groups_not_enabled(
                    _g("builtin_tool_ids", None) or [], _g("builtin_allowed_tools", None)
                )
                if not groups:
                    return "None"
                return ", ".join(groups) + " (add the group to builtin_tool_ids, or drop the entry)"

            def _fmt_integration_bindings() -> str:
                if not raw_bindings:
                    return "None"
                # Sort providers and ids so the read output is stable across
                # calls (dict insertion order is otherwise non-deterministic),
                # keeping read-modify-write diffs clean for humans and tooling.
                pairs: list[str] = []
                for provider in sorted(raw_bindings):
                    for sid in sorted(str(i) for i in (raw_bindings[provider] or [])):
                        label = f"`{provider}:{sid}`"
                        if sid in integ_names:
                            label += f" ({integ_names[sid]})"
                        if str(safe_uuid(sid)) in _read_only_ids:
                            label += " [read-only]"
                        if _signature := override_label(_signature_overrides.get(str(safe_uuid(sid)))):
                            label += f" [{_signature}]"
                        if _live := live_writes_label(_live_overrides.get(str(safe_uuid(sid)))):
                            label += f" [{_live}]"
                        if sid in integ_slugs:
                            label += f" [tool suffix `__{integ_slugs[sid]}`]"
                        if str(safe_uuid(sid)) in _orphaned_ids:
                            label += " [orphaned: owner no longer has access, runs refuse its tools]"
                        pairs.append(label)
                return f"{len(pairs)} — " + ", ".join(pairs)

            # Count KBs the agent can WRITE to (bound + flagged "write"), so the
            # tool surfaces the same store_knowledge grant the UI shows. Computed
            # inline from the two fields (not via the model property) so the line
            # is correct for any agent shape the loader returns.
            _kb_perms = getattr(agent, "knowledge_base_permissions", None) or {}
            _writable_kb_count = sum(
                1
                for k in (agent.knowledge_base_ids or [])
                if _kb_perms.get(str(k)) == "write"
            )

            def _fmt_kbs() -> str:
                if not kb_id_list:
                    return "None"
                items = []
                for i in kb_id_list:
                    label = f"{kb_names[i]} (`{i}`)" if i in kb_names else f"`{i}`"
                    if _kb_perms.get(i) == "write":
                        label += " [writable]"
                    items.append(label)
                suffix = (
                    f" ({_writable_kb_count} writable)" if _writable_kb_count else ""
                )
                return f"{len(kb_id_list)}{suffix} — " + ", ".join(items)

            # ``getattr`` defaults keep the read tool resilient against partial
            # agent doubles / rows that predate a column. Every editable option
            # is surfaced so the agent can faithfully read-modify-write ANY field
            # via update_agent (not just the handful that used to be shown).
            def _g(attr: str, default=None):
                return getattr(agent, attr, default)

            def _yn(value) -> str:
                return "Yes" if value else "No"

            def _fmt_list(values) -> str:
                vals = [str(v) for v in (values or []) if v not in (None, "")]
                return ", ".join(vals) if vals else "None"

            def _fmt_json(value) -> str:
                # Compact, copy-pasteable JSON so the agent can edit-and-resend
                # structured fields (config, scoping maps, pipeline refs). Only
                # ``None`` renders as "None" — an empty object/list is a
                # meaningful, distinct persisted value (e.g. output_schema or
                # config can legitimately be ``{}``), so it is rendered verbatim
                # to keep the read faithful for read-modify-write.
                if value is None:
                    return "None"
                try:
                    return json.dumps(value, ensure_ascii=False, sort_keys=True)
                except (TypeError, ValueError):
                    return str(value)

            def _fmt_channels() -> str:
                # NULL available_channel_types = all channels (legacy default).
                act = _g("available_channel_types", None)
                return "All channels" if act is None else (_fmt_list(act))

            _agent_max_iterations = _g("max_iterations", None)
            _temperature = _g("temperature", None)
            # The organization summary this agent's chat turns carry (#11533).
            from app.services.organization_context_injection import effective_for

            _org_context_on = await effective_for(_db, agent=agent)
            # web_token authenticates the public ``/public/chat/{web_token}``
            # endpoint, so it is a delivery credential, not a free-form config
            # knob. Surface the RAW token only to callers who can MANAGE the
            # agent (admin / owner / config-tier group member); every other
            # caller keeps the redacted "(token set)" indicator. This mirrors
            # the get_assignment webhook_secret contract: this builder read
            # tool has WIDER reach than the credential's manage tier (it serves
            # any visible agent — incl. company-managed agents readable by all
            # users and anonymous callers) and its output lands on the LLM/chat
            # surface, so it must redact at least as aggressively as the manage
            # tier or it becomes a credential-disclosure bypass. Manage-tier
            # callers still get the real token they need to build/verify the
            # embed URL (faithful read-modify-write for the field they own).
            _web_token = _g("web_token", None)
            _web_token_visible = False
            if _web_token and uid:
                from app.api.v1.helpers import can_manage

                _token_caller = await _load_caller_user_for_builder(_db, uid)
                _web_token_visible = _token_caller is not None and can_manage(
                    _token_caller, agent, group_ids
                )

            # Self fan-out: the stored setting and what it does once the
            # platform switch and the delegation depth are applied.
            from app.services.builtin_tools.handover import self_fanout_effective_text

            _self_fanout_stored = _g("allow_self_fanout", True) is not False
            _self_fanout_effective = self_fanout_effective_text(_self_fanout_stored)

            # Who the agent is shared with, named: groups with their member
            # count and people by name and email, each with its tier and id.
            # A caller with no account reads the company-wide tier alone, whose
            # agents carry no grant; it is handed the ids as stored.
            if uid:
                _sharing_lines = await _agent_audience_lines(_db, agent)
            else:
                _sharing_lines = [
                    f"  • Shared group IDs: {_fmt_list(_g('shared_group_ids', None))}",
                    f"  • Shared user IDs: {_fmt_list(_g('shared_user_ids', None))}",
                ]

            parts = [
                f"**{agent.name}**",
                f"  • ID: `{agent.id}`",
                f"  • Slug: `{agent.slug}`",
                f"  • Description: {agent.description or 'None'}",
                f"  • Icon: {_g('icon', None) or 'Bot'}",
                f"  • Enabled: {_yn(agent.enabled)}",
                f"  • Disable chat: {_yn(_g('disable_chat', False))}",
                f"  • Tags: {_fmt_list(agent.tags)}",
                f"  • Assignments: {wf_count}",
                "",
                "Behavior:",
                f"  • Output format: {_g('output_format', None) or 'markdown'}",
                f"  • Output schema: {_fmt_json(_g('output_schema', None))}",
                f"  • Accept a format requested by the caller: {_yn(_g('accept_caller_output_format', False) is True)}",
                f"  • Skip LLM (pipeline-only): {_yn(_g('skip_llm', False))}",
                f"  • Prepare tool: {_fmt_json(_g('prepare_tool_ref', None))}",
                f"  • Finalize tool: {_fmt_json(_g('finalize_tool_ref', None))}",
                "",
                "Model:",
                *(
                    ["  • LLM model source: platform routing model"]
                    if _g("llm_model_source", None) == "routing"
                    else []
                ),
                f"  • LLM model: {_g('llm_model', None) or 'Inherit (platform/user default)'}",
                f"  • LLM provider: {_g('llm_provider', None) or 'Auto'}",
                f"  • LLM provider ID: {_g('llm_provider_id', None) or 'Auto'}",
                f"  • Temperature: {_temperature if _temperature is not None else 'Provider default'}",
                f"  • Reasoning effort: {_g('reasoning_effort', None) or 'Default'}",
                f"  • Organization context: {_g('organization_context_mode', None) or 'inherit'}"
                f" (effective: {'on' if _org_context_on else 'off'})",
                "  • Organization categories preloaded: "
                + (
                    "inherit"
                    if _g("organization_context_categories", None) is None
                    else (", ".join(_g("organization_context_categories", None)) or "none")
                ),
                # max_iterations is the per-agent agentic-loop cap; NULL falls
                # back to the platform default (an assignment can override it).
                "  • Max iterations: "
                + (
                    str(_agent_max_iterations)
                    if _agent_max_iterations is not None
                    else "Platform default"
                ),
                "",
                "Tools & resources:",
                f"  • Knowledge bases: {_fmt_kbs()}",
                f"  • MCP servers: {_fmt_named(mcp_id_list, mcp_names)}",
                f"  • Custom tools: {_fmt_named(ct_id_list, ct_names)}",
                f"  • Integration bindings: {_fmt_integration_bindings()}",
                f"  • Integration template bindings: {_fmt_list(_g('integration_template_bindings', None))}",
                f"  • Integration template read-only: {_fmt_list(_g('integration_template_read_only', None))}",
                f"  • Built-in tools: {_fmt_list([describe_builtin_tool_id(t) for t in (_g('builtin_tool_ids', None) or [])] or None)}",
                f"  • Tool deny list: {_fmt_list(_g('tool_deny_list', None))}",
                f"  • Integration allowed tools: {_fmt_json(_g('integration_allowed_tools', None))}"
                + (
                    " [unknown tool names: "
                    + "; ".join(f"{k}: {', '.join(v)}" for k, v in sorted(_stale_scope_names.items()))
                    + "]"
                    if _stale_scope_names
                    else ""
                ),
                f"  • MCP allowed tools: {_fmt_json(_g('mcp_allowed_tools', None))}",
                f"  • Built-in allowed tools: {_fmt_json(_g('builtin_allowed_tools', None))}",
                f"  • Built-in tools outside the allowlist: {_fmt_outside_builtin_allowlist()}",
                f"  • Built-in allowlist for groups not enabled (granted nothing): {_fmt_allowlist_not_enabled()}",
                f"  • Integration element bindings: {_fmt_json(_g('integration_element_bindings', None))}",
                f"  • Integration element enforcement: {_fmt_list(_g('integration_element_enforcement', None))}",
                f"  • Prompts: {_fmt_named(prompt_ids, prompt_names)}",
                f"  • Skills: {_fmt_named(skill_ids, skill_names)}",
                "",
                "Handover:",
                f"  • Handover routing: {_fmt_handovers()}",
                f"  • Parallel copies of itself (agent setting): {_yn(_self_fanout_stored)}",
                f"  • Parallel copies of itself (effective): {_self_fanout_effective}",
                "",
                "Memory:",
                f"  • Short-term: {_yn(_g('memory_short_term_enabled', True))}",
                f"  • Medium-term: {_yn(_g('memory_medium_term_enabled', True))}",
                f"  • Persistent: {_yn(_g('memory_persistent_enabled', True))}",
                f"  • User memory: {_yn(_g('memory_user_enabled', True))}",
                "",
                "Human-in-the-loop:",
                f"  • Enabled: {_yn(_g('human_in_the_loop', False))}",
                "",
                "Channels & web:",
                f"  • Available channels: {_fmt_channels()}",
                f"  • Channel config: {_fmt_json(_g('channel_config', None))}",
                f"  • Web widget enabled: {_yn(_g('web_enabled', False))}"
                + (
                    f" (web_token: `{_web_token}`)"
                    if _web_token and _web_token_visible
                    else (" (token set)" if _web_token else "")
                ),
                f"  • Web suggested prompts: {_fmt_list(_g('web_suggested_prompts', None))}",
                "",
                "Visibility & sharing:",
                f"  • Company-managed: {_yn(agent.is_company_managed)}",
                f"  • Public catalog: {_yn(_g('is_public_catalog', False))}",
                *_sharing_lines,
                f"  • Config (free-form): {_fmt_json(_g('config', None))}",
            ]

            _created = _g("created_at", None)
            _updated = _g("updated_at", None)
            if _created or _updated:
                parts.append("")
                if _created:
                    parts.append(f"  • Created: {_created}")
                if _updated:
                    parts.append(f"  • Updated: {_updated}")

            # Surface long free-text fields in FULL (never truncated) so the
            # agent can faithfully inspect and rewrite them — mirrors
            # get_assignment, which surfaces its prompts in full for the same
            # reason. A preview made every read-before-write edit lossy. The
            # agent loop's per-result cap (_truncate_tool_result) remains the
            # context-safety net for unusually large values.
            _guardrails = _g("guardrails", None)
            _hitl_prompt = _g("human_in_the_loop_prompt", None)
            if _guardrails:
                parts.append("")
                parts.append("Guardrails (full):")
                parts.append(_guardrails)
            if _hitl_prompt:
                parts.append("")
                parts.append("Human-in-the-loop prompt (full):")
                parts.append(_hitl_prompt)
            # The intents a connected platform offers this agent under
            # (global bindings and the caller's own overrides).
            from .intent_bindings import agent_binding_lines, catalog_eligibility

            _intent_lines = await agent_binding_lines(_db, agent, uid) or []
            parts.append("")
            parts.append("Intent bindings:")
            parts.extend(f"  • {line}" for line in _intent_lines)
            if not _intent_lines:
                parts.append("  • None (no connected platform offers this agent)")
            elif (_not_listed := catalog_eligibility(agent)) is not None:
                parts.append(f"  • Not in the intent catalog while {_not_listed}")
            parts.append("")
            parts.append("Persona (full):")
            parts.append(agent.persona or "None")
            return "\n".join(project_get_agent_sections(parts, wanted_sections))

    # ── list_groups ───────────────────────────────────────────────────

    async def _list_groups(
        search: str = "",
        members: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """The platform's groups, as ``GET /groups`` serves every user: name,
        description, member count, and whether the caller belongs to it. The
        members themselves are ``GET /groups/{id}/members``, an admin read:
        listed for an admin's agent (the agent tools' one admin bypass), counted
        for everyone else.
        """
        from sqlalchemy import func, select

        from app.api.v1.helpers import get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.group import Group, GroupMember
        from app.models.user import User
        from app.services.share_principals import person_label

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Group listing is not available for anonymous users."

        limit, offset, page_err = _coerce_page_args(limit, offset, default_limit=50)
        if page_err:
            return page_err
        if not isinstance(search, str):
            return f"search must be a string (got {type(search).__name__})."
        _want_members, _members_err, _members_given = _parse_bool_arg(members, "members")
        if _members_err:
            return _members_err
        want_members = bool(_members_given and _want_members)

        async with _builder_session() as _db:
            member_count = (
                select(func.count(GroupMember.id))
                .where(GroupMember.group_id == Group.id)
                .correlate(Group)
                .scalar_subquery()
            )
            q = select(Group, member_count.label("member_count"))
            term = search.strip().lower()
            if term:
                q = q.where(func.lower(Group.name).contains(term, autoescape=True))
            total = (await _db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
            rows = (
                await _db.execute(q.order_by(func.lower(Group.name).asc()).limit(limit).offset(offset))
            ).all()
            own_group_ids = set(await get_user_group_ids(_db, uid))

            members_by_group: dict[str, list[str]] = {}
            members_note = ""
            members_listed = False
            if want_members and rows:
                caller = await _load_caller_user_for_builder(_db, uid)
                if agent_tool_admin_bypass(caller):
                    members_listed = True
                    member_rows = await _db.execute(
                        select(GroupMember.group_id, User.full_name, User.email, User.is_active)
                        .join(User, GroupMember.user_id == User.id)
                        .where(GroupMember.group_id.in_([g.id for g, _count in rows]))
                        .order_by(func.lower(User.full_name).asc(), User.email.asc())
                    )
                    for group_id, full_name, email, is_active in member_rows.all():
                        label = person_label(full_name, email) + ("" if is_active else " (deactivated)")
                        members_by_group.setdefault(str(group_id), []).append(label)
                else:
                    members_note = (
                        "\nMembers are listed for administrators only; each group shows "
                        "its member count. To reach named people outside a group, share "
                        "with them directly (update_agent users=...)."
                    )

        if not rows:
            scope = f" matching '{search.strip()}'" if term else ""
            return f"No groups{scope} on the platform."

        lines = [f"Groups ({total} total, showing {len(rows)} from offset {offset}):"]
        for group, count in rows:
            count = int(count or 0)
            line = f"- **{group.name}** (`{group.id}`) — {count} member{'' if count == 1 else 's'}"
            if str(group.id) in own_group_ids:
                line += ", you are a member"
            if group.description:
                line += f" — {group.description}"
            if members_listed:
                listed = members_by_group.get(str(group.id), [])
                shown = ", ".join(listed[:_MAX_GROUP_MEMBERS_LISTED]) or "none"
                more = len(listed) - _MAX_GROUP_MEMBERS_LISTED
                line += f"\n  Members: {shown}" + (f" and {more} more" if more > 0 else "")
            lines.append(line)
        if members_note:
            lines.append(members_note)
        lines.append(
            "\nShare an agent with a group through update_agent shared_group_ids "
            "(the group id), or with named people through update_agent users (their emails)."
        )
        return "\n".join(lines)

    # ── list_agents ───────────────────────────────────────────────────

    async def _list_agents(
        search: str = "",
        scope: str = "visible",
        limit: int = 50,
        offset: int = 0,
        detail: str = "summary",
    ) -> str:
        from sqlalchemy import func, select

        from app.api.v1.helpers import (
            caller_visible_filter,
            get_user_group_ids,
            resolve_owner_names,
            user_visible_filter,
        )
        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.assignment import Assignment

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        limit, offset, page_err = _coerce_page_args(
            limit, offset, default_limit=50
        )
        if page_err:
            return page_err

        scope = (scope or "visible").strip().lower()
        if scope not in ("visible", "all"):
            return (
                f"Invalid scope `{scope}`. Use 'visible' (default, entities "
                "you own / shared with you / company-managed) or 'all' "
                "(admin only, every agent on the platform)."
            )

        detail = (detail or "summary").strip().lower()
        if detail not in ("summary", "full"):
            return (
                f"Invalid detail `{detail}`. Use 'summary' (default — name, "
                "id, enabled status, assignment count) or 'full' (adds "
                "owner, created timestamp, integration bindings, knowledge "
                "bases, and web/channel exposure per agent)."
            )
        full_detail = detail == "full"

        async with _builder_session() as _db:
            admin_all = False
            if scope == "all":
                caller = (
                    await _load_caller_user_for_builder(_db, uid) if uid else None
                )
                if not agent_tool_admin_bypass(caller):
                    return (
                        "Only admins can list ALL agents across the platform "
                        "(and an admin's agent cannot while "
                        "AGENT_TOOLS_STRICT_ADMIN_ACCESS is set). Call "
                        "list_agents without scope (or scope='visible') to see "
                        "the agents you have access to."
                    )
                admin_all = True

            # The ``is_admin`` bypass ignores group ids, so skip the group
            # membership query on the admin-wide path.
            group_ids = (
                await get_user_group_ids(_db, uid) if uid and not admin_all else []
            )

            if admin_all:
                # Admin-requested platform-wide listing — mirrors the REST
                # admin contract (``GET /agents/all`` and the ``is_admin``
                # bypass in ``user_visible_filter``). Explicit opt-in only:
                # the default scope stays the caller's three-tier visibility.
                visibility = user_visible_filter(
                    Agent, uid, group_ids, is_admin=True
                )
            else:
                visibility = caller_visible_filter(Agent, uid, group_ids)

            base = select(Agent).where(visibility)
            if search:
                base = base.where(Agent.name.ilike(f"%{search}%"))

            total = (
                await _db.execute(
                    select(func.count()).select_from(base.subquery())
                )
            ).scalar() or 0
            if not total:
                return "No agents found."

            result = await _db.execute(
                base.order_by(Agent.name, Agent.id).limit(limit).offset(offset)
            )
            agents = result.scalars().all()
            if not agents:
                return (
                    f"No agents on this page (offset {offset} of {total} "
                    "total). Use a smaller offset."
                )

            agent_ids = [a.id for a in agents]
            count_q = (
                select(Assignment.agent_id, func.count().label("cnt"))
                .where(Assignment.agent_id.in_(agent_ids))
                .group_by(Assignment.agent_id)
            )
            count_result = await _db.execute(count_q)
            wf_counts: dict = {row[0]: row[1] for row in count_result.fetchall()}

            # In the admin-wide view, show who owns each agent so "agents I
            # don't own" are identifiable at a glance. The full-detail view
            # also resolves owners: bulk reconciliation workloads (e.g.
            # syncing an external inventory) key rows on (name, owner).
            owner_names = (
                await resolve_owner_names(_db, agents)
                if (admin_all or full_detail)
                else {}
            )

            # Full detail: batch-resolve bound-resource names for the WHOLE
            # page in two queries (KBs + integrations) instead of forcing one
            # get_agent round-trip per agent — the N+1 pattern that made
            # platform-wide inventory reconciliation cost 600+ tool calls.
            # Name resolution honours the same visibility gate as get_agent:
            # a visible agent row may reference a resource the caller cannot
            # see, in which case only the bare id is shown (never the name).
            kb_names: dict[str, str] = {}
            integ_names: dict[str, str] = {}
            if full_detail:
                from app.core.utils import safe_uuid
                from app.models.integration import Integration
                from app.models.knowledge_base import KnowledgeBase

                def _detail_visible_clause(model):
                    if admin_all:
                        return user_visible_filter(
                            model, uid, group_ids, is_admin=True
                        )
                    return caller_visible_filter(model, uid, group_ids)

                page_kb_ids = {
                    u
                    for a in agents
                    for k in (getattr(a, "knowledge_base_ids", None) or [])
                    if (u := safe_uuid(str(k)))
                }
                page_integ_ids = {
                    u
                    for a in agents
                    for ids in (
                        getattr(a, "integration_bindings", None) or {}
                    ).values()
                    for i in (ids or [])
                    if (u := safe_uuid(str(i)))
                }
                if page_kb_ids:
                    kb_rows = await _db.execute(
                        select(KnowledgeBase.id, KnowledgeBase.name).where(
                            KnowledgeBase.id.in_(page_kb_ids),
                            _detail_visible_clause(KnowledgeBase),
                        )
                    )
                    kb_names = {str(r[0]): r[1] for r in kb_rows.all()}
                if page_integ_ids:
                    integ_rows = await _db.execute(
                        select(Integration.id, Integration.name).where(
                            Integration.id.in_(page_integ_ids),
                            _detail_visible_clause(Integration),
                        )
                    )
                    integ_names = {str(r[0]): r[1] for r in integ_rows.all()}

            def _detail_lines(a) -> list[str]:
                from app.api.v1.integration_catalog import PROVIDER_CATALOG

                lines: list[str] = []
                created = getattr(a, "created_at", None)
                if created:
                    lines.append(f"  Created: {created}")

                bindings = getattr(a, "integration_bindings", None) or {}
                if bindings:
                    entries: list[str] = []
                    for provider in sorted(bindings):
                        generic = PROVIDER_CATALOG.get(provider, {}).get(
                            "name", provider
                        )
                        instances = [
                            integ_names.get(sid, f"`{sid}`")
                            for sid in sorted(
                                str(i) for i in (bindings[provider] or [])
                            )
                        ]
                        suffix = f": {', '.join(instances)}" if instances else ""
                        entries.append(f"{generic} [`{provider}`]{suffix}")
                    lines.append(f"  Integrations: {'; '.join(entries)}")
                else:
                    lines.append("  Integrations: None")

                kb_ids = [
                    str(k)
                    for k in (getattr(a, "knowledge_base_ids", None) or [])
                    if k
                ]
                if kb_ids:
                    kb_items = [
                        (f"{kb_names[k]} (`{k}`)" if k in kb_names else f"`{k}`")
                        for k in kb_ids
                    ]
                    lines.append(f"  Knowledge bases: {', '.join(kb_items)}")
                else:
                    lines.append("  Knowledge bases: None")

                web = "Yes" if getattr(a, "web_enabled", False) else "No"
                act = getattr(a, "available_channel_types", None)
                channels = (
                    "All channels"
                    if act is None
                    else (", ".join(str(c) for c in act) if act else "None")
                )
                catalog = (
                    "; public catalog: Yes"
                    if getattr(a, "is_public_catalog", False)
                    else ""
                )
                lines.append(
                    f"  Exposure: web widget: {web}; channels: {channels}"
                    f"{catalog}"
                )
                return lines

            scope_label = "all agents (admin)" if admin_all else "visible agents"
            detail_label = ", full detail" if full_detail else ""
            first = offset + 1
            last = offset + len(agents)
            parts: list[str] = [
                f"Found {total} agent(s) [{scope_label}{detail_label}] — "
                f"showing {first}-{last}:\n"
            ]
            for a in agents:
                cm = " [company-managed]" if a.is_company_managed else ""
                owner = ""
                if (admin_all or full_detail) and a.user_id is not None:
                    owner_name = owner_names.get(str(a.user_id), str(a.user_id))
                    owner = f" [owner: {owner_name}]"
                enabled = "enabled" if a.enabled else "disabled"
                wf_count = wf_counts.get(a.id, 0)
                block = (
                    f"• **{a.name}** (id: `{a.id}`, {enabled}, "
                    f"{wf_count} assignments){cm}{owner}\n"
                    f"  {a.description or 'No description'}"
                )
                if full_detail:
                    block += "\n" + "\n".join(_detail_lines(a))
                parts.append(block)
            if last < total:
                parts.append(
                    f"\n{total - last} more agent(s). Call list_agents again "
                    f"with offset={last} for the next page."
                )
            return "\n".join(parts)

    # ── update_agent ──────────────────────────────────────────────────

    async def _update_agent(
        agent_id: str,
        name: str = "",
        slug: str = "",
        description: str = "",
        persona: str = "",
        guardrails: str = "",
        enabled: str = "",
        tags: str = "",
        icon: str = "",
        output_format: str = "",
        output_schema: str = "",
        llm_model: str = "",
        llm_provider: str = "",
        llm_provider_id: str = "",
        llm_model_source: str = "",
        temperature: str = "",
        reasoning_effort: str = "",
        organization_context_mode: str = "",
        organization_context_categories: object = None,
        knowledge_base_ids: str = "",
        knowledge_base_permissions: str = "",
        mcp_server_ids: str = "",
        custom_tool_ids: str = "",
        builtin_tool_ids: str = "",
        tool_deny_list: str = "",
        prompt_ids: str = "",
        skill_ids: str = "",
        integration_bindings: str = "",
        integration_template_bindings: str = "",
        integration_allowed_tools: str = "",
        mcp_allowed_tools: str = "",
        builtin_allowed_tools: str = "",
        integration_element_bindings: str = "",
        integration_element_enforcement: str = "",
        integration_read_only: str = "",
        integration_template_read_only: str = "",
        integration_signatures: object = "",
        integration_live_writes: object = "",
        handover_agent_ids: str = "",
        handover_types: str = "",
        handover_conditions: str = "",
        allow_self_fanout: str = "",
        accept_caller_output_format: str = "",
        prepare_tool_ref: str = "",
        finalize_tool_ref: str = "",
        skip_llm: str = "",
        memory_short_term_enabled: str = "",
        memory_medium_term_enabled: str = "",
        memory_persistent_enabled: str = "",
        memory_user_enabled: str = "",
        human_in_the_loop: str = "",
        human_in_the_loop_prompt: str = "",
        available_channel_types: str = "",
        channel_config: str = "",
        web_enabled: str = "",
        web_suggested_prompts: str = "",
        is_public_catalog: str = "",
        catalog_include_personal_tools: str = "",
        disable_chat: str = "",
        shared_group_ids: str = "",
        shared_group_permissions: str = "",
        users: object = "",
        users_permission: str = "",
        config: str = "",
        is_company_managed: str = "",
        max_iterations: str = "",
        wait_policy: object = "",
        binding_mode: str = "replace",
        mode: str = "replace",
        field: str = "",
        edits: list[dict[str, object]] | None = None,
        section: str = "",
        content: str = "",
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.general_assistant_config import (
            MAX_ITERATIONS_CEILING,
            clamp_max_iterations,
        )
        from app.services.skill_edits import SkillEditError, partial_mode_args, partial_text_update

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Agent updates are not available for anonymous users."

        agent_uuid, id_err = _validate_id_arg(agent_id, "agent_id")
        if id_err:
            return id_err

        # A partial mode edits one stored text field (find/replace, append, one
        # section) instead of resending it whole: the helper update_skill uses.
        try:
            text_mode, text_field = partial_mode_args(
                mode,
                field,
                fields=AGENT_PARTIAL_TEXT_FIELDS,
                whole={"persona": persona, "guardrails": guardrails, "description": description},
                content=content,
            )
        except SkillEditError as exc:
            return f"Error: {exc} Nothing was saved."

        # Intentionally avoid the shared build_agent_builder_tool session.
        # A dedicated session per invocation prevents commit races when the LLM
        # emits multiple update_agent calls in parallel.
        async with _builder_session() as _db:
            # A model choice is serialized with provider deletions,
            # like the REST update: shared lock, before the agent is read.
            if any(
                isinstance(value, str) and value.strip()
                for value in (llm_model, llm_provider, llm_provider_id)
            ):
                from app.services.llm_provider_deletion import (
                    ProviderLockBusyError,
                    lock_provider_configs,
                )

                try:
                    await lock_provider_configs(_db, shared=True)
                except ProviderLockBusyError as exc:
                    return exc.detail
            # Mirrors the REST API's ``update_agent`` gates: built-in
            # agents stay read-only in XTM One mode, company-managed
            # agents require admin, group-shared agents require
            # ``config``-tier permission. The previous
            # ``Agent.user_id == uid`` filter was both too lax (it didn't
            # block built-in modifications when the user happened to be
            # the legacy owner) and too strict (it blocked admins from
            # editing company-managed agents that the UI lets them edit).
            agent, perm_err = await _load_writable_agent_for_builder(
                _db, uid, agent_uuid, action="edit"
            )
            if perm_err is not None or agent is None:
                return perm_err or (
                    f"Agent `{agent_id}` not found or you don't have "
                    "permission to edit it."
                )

            from app.services.agent_config_versioning import (
                INITIAL_COMMIT_MESSAGE,
                record_agent_version_changes,
                versioned_agent_values,
            )
            from app.services.prompt_versioning import AgentVersionLockBusyError

            # What the REST update versions, versioned here too.
            versioned_before = versioned_agent_values(agent)
            updated: list[str] = []
            text_read_back = ""
            if text_mode != "replace":
                try:
                    new_text, text_read_back = partial_text_update(
                        text_mode,
                        getattr(agent, text_field),
                        field=text_field,
                        content=content,
                        section=section,
                        edits=edits,
                    )
                except SkillEditError as exc:
                    return f"Error: {exc} Nothing was saved."
                # Stored stripped, as the whole-text path stores these fields.
                setattr(agent, text_field, new_text.strip() or None)
                updated.append(f"{text_field} ({text_mode})")

            # Tool kwargs are forwarded verbatim from JSON/LLM output, so a
            # field the schema types as a string can still arrive as a native
            # number / bool / list / dict and crash the ``(x or "").strip()``
            # (and ``.lower()`` / ``.split()``) guards below with an uncaught
            # ``AttributeError`` — a 500-style tool crash (e.g.
            # ``llm_provider_id=123``). Validate the string-typed args up front
            # and hand back a friendly, correctable error instead (same
            # hardening as the ``max_iterations`` isinstance guard).
            # Fields routed through the non-string-safe parsers — temperature,
            # the boolean flags, is_public_catalog, output_schema, config, the
            # pipeline/scoping JSON maps, shared_group_permissions,
            # knowledge_base_permissions, channel_config, handover_conditions,
            # max_iterations and agent_id — are intentionally NOT listed: those
            # helpers accept native JSON types on purpose.
            for _sa_name, _sa_val in (
                ("name", name),
                ("slug", slug),
                ("description", description),
                ("persona", persona),
                ("guardrails", guardrails),
                ("enabled", enabled),
                ("tags", tags),
                ("icon", icon),
                ("output_format", output_format),
                ("llm_model", llm_model),
                ("llm_provider", llm_provider),
                ("llm_provider_id", llm_provider_id),
                ("llm_model_source", llm_model_source),
                ("reasoning_effort", reasoning_effort),
                ("organization_context_mode", organization_context_mode),
                ("organization_context_categories", organization_context_categories),
                ("knowledge_base_ids", knowledge_base_ids),
                ("mcp_server_ids", mcp_server_ids),
                ("custom_tool_ids", custom_tool_ids),
                ("prompt_ids", prompt_ids),
                ("skill_ids", skill_ids),
                ("handover_agent_ids", handover_agent_ids),
                ("handover_types", handover_types),
                ("builtin_tool_ids", builtin_tool_ids),
                ("tool_deny_list", tool_deny_list),
                ("integration_bindings", integration_bindings),
                ("integration_template_bindings", integration_template_bindings),
                ("integration_template_read_only", integration_template_read_only),
                ("integration_element_enforcement", integration_element_enforcement),
                ("human_in_the_loop_prompt", human_in_the_loop_prompt),
                ("available_channel_types", available_channel_types),
                ("web_suggested_prompts", web_suggested_prompts),
                ("shared_group_ids", shared_group_ids),
                ("users_permission", users_permission),
                ("is_company_managed", is_company_managed),
                ("binding_mode", binding_mode),
            ):
                if _sa_val is not None and not isinstance(_sa_val, str):
                    return (
                        f"{_sa_name} must be a string (got "
                        f"{type(_sa_val).__name__}). Pass it as a string; use "
                        "'none'/'clear' to clear a clearable field."
                    )
            # Refused before anything is written, like the REST body's Literal.
            _model_source = (llm_model_source or "").strip().lower()
            if _model_source not in ("", "routing", "none", "clear"):
                return (
                    "Invalid llm_model_source: use 'routing' to run the agent on "
                    "the platform routing model, or 'none' to return it to its "
                    "own model."
                )
            # On the routing model the agent keeps no model of its own, so a
            # model passed with it would be dropped once written;
            # 'none'/'clear' empties a field and agrees with the source.
            if _model_source == "routing":
                _own_model = [
                    _name
                    for _name, _val in (
                        ("llm_model", llm_model),
                        ("llm_provider", llm_provider),
                        ("llm_provider_id", llm_provider_id),
                    )
                    if (_val or "").strip().lower() not in ("", "none", "clear")
                ]
                if _own_model:
                    return (
                        "llm_model_source 'routing' runs the agent on the platform "
                        "routing model, which keeps no model of its own: leave "
                        f"{', '.join(_own_model)} empty, or leave llm_model_source "
                        "empty to pick the agent's own model."
                    )

            # How binding fields (knowledge_base_ids, mcp_server_ids,
            # custom_tool_ids, prompt_ids, skill_ids, handover_agent_ids,
            # integration_bindings, and the people in ``users``) are applied.
            # ``replace`` (default) keeps the
            # legacy full-set semantics; ``add`` / ``remove`` merge or subtract
            # the supplied ids/pairs against the agent's CURRENT set, so a single
            # binding can be changed without re-sending the whole set. Without
            # this, adding one integration to an agent meant reading it back
            # (which only ever returned a COUNT), reconstructing every existing
            # binding by hand, and re-writing the full set — error-prone enough
            # to silently drop or swap bindings.
            binding_op = (binding_mode or "").strip().lower() or "replace"
            if binding_op not in ("replace", "add", "remove"):
                return (
                    "Invalid binding_mode: must be 'replace' (default), 'add', "
                    "or 'remove'."
                )

            # Free-text fields are guarded on ``.strip()`` truthiness so a
            # whitespace-only value is treated as "leave unchanged" (it can't
            # persist a blank name/description/persona/icon), and the stored
            # value is canonicalised to the stripped form — same hardening as
            # ``llm_model`` and the binding fields below.
            if (name or "").strip():
                agent.name = (name or "").strip()
                updated.append("name")
            if (description or "").strip():
                agent.description = (description or "").strip()
                updated.append("description")
            if (persona or "").strip():
                agent.persona = (persona or "").strip()
                updated.append("persona")
            if enabled and enabled.lower() in ("true", "false"):
                agent.enabled = enabled.lower() == "true"
                updated.append("enabled")
            if tags:
                agent.tags = [t.strip() for t in tags.split(",") if t.strip()]
                updated.append("tags")
            if (icon or "").strip():
                agent.icon = (icon or "").strip()
                updated.append("icon")
            if (output_format or "").strip():
                # Canonicalise to the stripped value so "  markdown  " is
                # accepted (a common LLM/UI formatting glitch) rather than
                # rejected as invalid; a whitespace-only value leaves it
                # unchanged.
                fmt = (output_format or "").strip()
                valid_formats = ("markdown", "html", "json", "plain_text")
                if fmt not in valid_formats:
                    return (
                        "Invalid output_format: must be one of "
                        f"{', '.join(valid_formats)}"
                    )
                agent.output_format = fmt
                updated.append("output_format")
            if (llm_model or "").strip():
                # "none" clears the override and falls back to platform default.
                # A whitespace-only value is treated as "leave unchanged" (like
                # the binding fields), and the stored model is canonicalised to
                # the stripped value so a stray LLM/UI value can't persist a
                # bogus, untrimmed model that breaks downstream routing.
                stripped_model = (llm_model or "").strip()
                if stripped_model.lower() == "none":
                    agent.llm_model = None
                else:
                    # Hard gate: the builder runs with LLM-supplied input, so
                    # a hallucinated or retired model id (e.g. a stale dated
                    # Anthropic snapshot) is a live risk — persisting one
                    # makes every call on the agent 404. Only models
                    # in a configured provider's curated ``available_models``
                    # catalog are accepted; the error message lists valid ids
                    # so the LLM can self-correct.
                    model_error = await _validate_selectable_chat_model_for_builder(
                        _db, stripped_model, user_id=uid, stored_model=agent.llm_model
                    )
                    if model_error:
                        return model_error
                    agent.llm_model = stripped_model
                updated.append("llm_model")

            # llm_provider_id — UUID of a specific configured provider row to
            # pin the agent to. Blank = leave unchanged; "none"/"clear" clears
            # it; otherwise it must be a valid UUID (mirrors REST, which sets
            # ``uuid.UUID(raw) if raw else None``). Handled BEFORE llm_provider
            # so clearing the provider below can also wipe a just-set id, exactly
            # like the REST endpoint's ordering.
            if (llm_provider_id or "").strip():
                _lpid = llm_provider_id.strip()
                if _lpid.lower() in ("none", "clear"):
                    agent.llm_provider_id = None
                else:
                    _lpid_uuid = safe_uuid(_lpid)
                    if _lpid_uuid is None:
                        return (
                            "Invalid llm_provider_id: must be a valid UUID, or "
                            "'none'/'clear' to clear it."
                        )
                    # Clearing the provider below clears this id too: it is
                    # never stored.
                    if (llm_provider or "").strip().lower() not in ("none", "clear"):
                        from app.services.llm_provider_deletion import (
                            ProviderLockBusyError,
                            provider_reference_problem,
                        )

                        try:
                            _lpid_problem = await provider_reference_problem(
                                _db,
                                _lpid_uuid,
                                stored=agent.llm_provider_id,
                                label="this agent's model",
                            )
                        except ProviderLockBusyError as exc:
                            return exc.detail
                        if _lpid_problem:
                            return _lpid_problem
                        # A pin the caller picks names a provider they may use
                        # unless their agent takes the admin bypass.
                        if _lpid_uuid != agent.llm_provider_id and not agent_tool_admin_bypass(
                            await _load_caller_user_for_builder(_db, uid)
                        ):
                            from app.llm.provider_access import (
                                RESTRICTED_PROVIDER_MESSAGE,
                                provider_id_usable_by,
                            )

                            if not await provider_id_usable_by(_db, _lpid_uuid, uid):
                                return RESTRICTED_PROVIDER_MESSAGE.format(
                                    label="this agent's model"
                                )
                    agent.llm_provider_id = _lpid_uuid
                updated.append("llm_provider_id")

            # llm_provider — provider key (e.g. 'anthropic', 'openai'). Blank =
            # leave unchanged; "none"/"clear" clears it AND the bound
            # llm_provider_id (mirrors REST: "when llm_provider is cleared, also
            # clear llm_provider_id"). No catalog validation here — same as REST,
            # which stores the provider string verbatim.
            if (llm_provider or "").strip():
                _lp = llm_provider.strip()
                if _lp.lower() in ("none", "clear"):
                    agent.llm_provider = None
                    agent.llm_provider_id = None
                    if "llm_provider_id" not in updated:
                        updated.append("llm_provider_id")
                else:
                    agent.llm_provider = _lp
                updated.append("llm_provider")

            # The model source, as on the REST update: an explicit
            # source wins; otherwise picking a model leaves the routing model,
            # so a chosen model is never ignored behind it. On the routing
            # model the agent keeps no model of its own.
            if _model_source:
                agent.llm_model_source = "routing" if _model_source == "routing" else None
                updated.append("llm_model_source")
            elif (
                getattr(agent, "llm_model_source", None)
                and any(f in updated for f in ("llm_model", "llm_provider", "llm_provider_id"))
                and (agent.llm_model or agent.llm_provider or agent.llm_provider_id)
            ):
                agent.llm_model_source = None
                updated.append("llm_model_source")
            if getattr(agent, "llm_model_source", None):
                agent.llm_model = None
                agent.llm_provider = None
                agent.llm_provider_id = None

            # max_iterations: "" / None = leave unchanged; "none"/"default"/
            # "inherit" clears the per-agent override (NULL = platform default);
            # a number (native int OR numeric string) sets the cap, clamped to
            # the shared ceiling. Tool kwargs are forwarded verbatim and the
            # schema allows an integer, so guard the sentinel check with
            # isinstance(str) and let _parse_optional_int_arg handle int/str —
            # a native int must never reach ``.strip()`` (mirrors
            # update_assignment).
            if isinstance(max_iterations, str) and max_iterations.strip().lower() in (
                "none",
                "default",
                "inherit",
            ):
                agent.max_iterations = None
                updated.append("max_iterations")
            else:
                _mi_val, _mi_err, _mi_provided = _parse_optional_int_arg(
                    max_iterations,
                    "max_iterations",
                    minimum=1,
                    maximum=MAX_ITERATIONS_CEILING,
                )
                if _mi_provided:
                    if _mi_err:
                        return _mi_err
                    agent.max_iterations = (
                        clamp_max_iterations(_mi_val)
                        if _mi_val is not None
                        else None
                    )
                    updated.append("max_iterations")

            # wait_policy: a JSON object narrows the platform wait
            # policy for this agent; "none" clears it (inherit the platform).
            _wp_val, _wp_err, _wp_provided = _parse_wait_policy_arg(wait_policy)
            if _wp_provided:
                if _wp_err:
                    return _wp_err
                agent.wait_policy = _wp_val
                updated.append("wait_policy")

            # Resource bindings — comma-separated UUID lists. With the default
            # binding_mode='replace' they REPLACE the binding; binding_mode
            # 'add'/'remove' merges or subtracts them against the current set
            # (see ``_merge_id_list``).  Clearing a binding requires the explicit
            # "none"/"clear" sentinel (replace mode only), mirroring the
            # ``llm_model`` "none" convention above.  An
            # empty or whitespace-only string means "leave unchanged" (the
            # ``.strip()`` guards below skip it), and a non-empty value that
            # contains no valid UUID token (e.g. ``","`` or ``", ,"``) is
            # rejected rather than treated as a clear — so neither a stray
            # whitespace value nor a stray-separator value can ever silently
            # wipe a binding.  Each id is validated as a UUID and canonicalised
            # (``str(uuid.UUID(...))``) so a non-UUID can't be persisted and
            # later blow up downstream ``uuid.UUID(id_str)`` conversions (REST
            # helpers, runtime).
            def _is_clear_sentinel(raw: str) -> bool:
                return raw.strip().lower() in ("none", "clear")

            def _csv_uuids(raw: str) -> tuple[list[str] | None, list[str]]:
                ids: list[str] = []
                invalid: list[str] = []
                for token in raw.split(","):
                    token = token.strip()
                    if not token:
                        continue
                    try:
                        ids.append(str(uuid.UUID(token)))
                    except ValueError:
                        invalid.append(token)
                if invalid:
                    return None, invalid
                return ids, []

            def _invalid_ids_error(label: str, invalid: list[str]) -> str:
                return (
                    f"Invalid {label}: {', '.join(invalid)} — each id must be a "
                    "valid UUID (comma-separated), or 'none'/'clear' to clear the "
                    "binding."
                )

            def _no_valid_ids_error(label: str) -> str:
                return (
                    f"No valid {label} found in the input — provide one or more "
                    "comma-separated UUIDs, or 'none'/'clear' to clear the binding."
                )

            def _sentinel_mode_error(label: str) -> str:
                return (
                    f"'none'/'clear' clears {label} and is only valid with "
                    "binding_mode='replace' (the default). In 'add'/'remove' "
                    "mode, pass the specific UUID(s) to add or remove."
                )

            def _merge_id_list(current: list | None, incoming: list[str]) -> list[str]:
                # binding_op 'add' = current + new (deduped, order-preserving);
                # 'remove' = current - new; 'replace' = incoming verbatim.
                cur = [str(x) for x in (current or [])]
                if binding_op == "add":
                    merged = list(cur)
                    for i in incoming:
                        if i not in merged:
                            merged.append(i)
                    return merged
                if binding_op == "remove":
                    drop = set(incoming)
                    return [i for i in cur if i not in drop]
                return incoming

            def _apply_bindings(
                current: dict | None, incoming: dict[str, list[str]]
            ) -> dict[str, list[str]]:
                # binding_op-aware merge/subtract for the {provider: [ids]} map.
                # 'remove' drops a provider key whose id list becomes empty.
                cur = {p: list(ids) for p, ids in (current or {}).items()}
                if binding_op == "add":
                    for prov, ids in incoming.items():
                        existing = cur.setdefault(prov, [])
                        for i in ids:
                            if i not in existing:
                                existing.append(i)
                    return cur
                if binding_op == "remove":
                    drop = {prov: set(ids) for prov, ids in incoming.items()}
                    result: dict[str, list[str]] = {}
                    for prov, ids in cur.items():
                        if prov in drop:
                            kept = [i for i in ids if i not in drop[prov]]
                            if kept:
                                result[prov] = kept
                        else:
                            result[prov] = ids
                    return result
                return incoming

            stored_handover_types = getattr(agent, "handover_types", None)
            _id_list_fields = (
                ("knowledge_base_ids", knowledge_base_ids),
                ("mcp_server_ids", mcp_server_ids),
                ("custom_tool_ids", custom_tool_ids),
                ("prompt_ids", prompt_ids),
                ("skill_ids", skill_ids),
                ("handover_agent_ids", handover_agent_ids),
            )
            for field_name, raw_value in _id_list_fields:
                if not (raw_value or "").strip():
                    continue
                if _is_clear_sentinel(raw_value):
                    # Clearing only makes sense in 'replace' mode; in add/remove
                    # mode the sentinel is rejected rather than silently treated
                    # as "remove everything".
                    if binding_op != "replace":
                        return _sentinel_mode_error(field_name)
                    setattr(agent, field_name, [])
                    updated.append(field_name)
                    continue
                ids, invalid = _csv_uuids(raw_value)
                if ids is None:
                    return _invalid_ids_error(field_name, invalid)
                if not ids:
                    # Non-empty, non-sentinel input that parsed to zero ids
                    # (e.g. "," or ", ,"): reject rather than silently wiping.
                    return _no_valid_ids_error(field_name)
                setattr(
                    agent,
                    field_name,
                    _merge_id_list(getattr(agent, field_name, None), ids),
                )
                updated.append(field_name)

            # Per-KB write access — "<kb_id>:write" / "<kb_id>:read" pairs
            # (read is the default, only write grants are retained), or
            # "none"/"clear" to reset all bound KBs to read-only. Mirrors the
            # REST ``PUT /agents/{id}`` ``knowledge_base_permissions`` contract
            # so the builder can grant ``store_knowledge`` on a bound KB — the
            # one KB capability the tool previously couldn't set. Parsed AFTER
            # ``knowledge_base_ids`` above so a grant set in the same call as
            # the binding is honoured by the prune below.
            perms, perm_err, perms_provided = _parse_kb_permissions_arg(
                knowledge_base_permissions
            )
            if perm_err:
                return perm_err
            if perms_provided:
                agent.knowledge_base_permissions = perms or None
                updated.append("knowledge_base_permissions")

            # Drop write grants whose KB is no longer bound — runs whenever the
            # binding set OR the grants changed, mirroring the REST endpoint's
            # ``prune_orphaned_knowledge_base_permissions`` call so a stale grant
            # can never linger after a KB is detached in the same update.
            if {"knowledge_base_ids", "knowledge_base_permissions"}.intersection(
                updated
            ):
                agent.prune_orphaned_knowledge_base_permissions()

            if (integration_bindings or "").strip():
                # Format: "provider:id,provider:id" → {provider: [ids]}.
                # A whitespace-only value is treated as "leave unchanged" (the
                # ``.strip()`` guard above), so it can't silently wipe the map.
                # With binding_mode='replace' (default) the parsed map REPLACES
                # the agent's bindings; 'add'/'remove' merge or subtract the
                # parsed pairs against the current map (see ``_apply_bindings``),
                # so a single integration can be attached/detached without
                # re-sending the whole set.  A literal "none"/"clear" empties all
                # bindings (replace mode only).  Malformed entries (bad shape or a
                # non-UUID integration id) are rejected with a clear error
                # *before* mutating the agent so a formatting slip can't silently
                # drop entries or wipe the existing bindings (the early return
                # skips the commit, so no partial change is persisted).
                _stored_integration_ids = flatten_binding_ids(
                    agent.integration_bindings
                )
                if integration_bindings.strip().lower() in ("none", "clear"):
                    if binding_op != "replace":
                        return _sentinel_mode_error("integration_bindings")
                    agent.integration_bindings = {}
                    updated.append("integration_bindings")
                else:
                    bindings: dict[str, list[str]] = {}
                    malformed: list[str] = []
                    for raw_pair in integration_bindings.split(","):
                        cleaned = raw_pair.strip()
                        if not cleaned:
                            continue
                        prov, sep, integ_id = cleaned.partition(":")
                        # Canonicalise the provider key to lower-case: downstream
                        # integration tool loading compares the binding key to
                        # ``Integration.provider`` (always a lower-case catalog
                        # key) with an exact match, so a value like "GitHub"
                        # would persist a binding that never resolves.
                        prov, integ_id = prov.strip().lower(), integ_id.strip()
                        if not sep or not prov or not integ_id:
                            malformed.append(cleaned)
                            continue
                        try:
                            integ_id = str(uuid.UUID(integ_id))
                        except ValueError:
                            malformed.append(cleaned)
                            continue
                        bindings.setdefault(prov, []).append(integ_id)
                    if malformed:
                        return (
                            "Invalid integration_bindings entry(ies): "
                            f"{', '.join(malformed)}. Use 'provider:integration_id' "
                            "pairs (integration_id must be a valid UUID) separated "
                            "by commas (e.g. 'github:UUID,slack:UUID'), or "
                            "'none'/'clear' to clear all bindings."
                        )
                    if not bindings:
                        # Non-empty, non-sentinel input that parsed to zero pairs
                        # (e.g. ","): reject rather than silently wiping the map.
                        return (
                            "No valid integration_bindings found in the input. Use "
                            "'provider:integration_id' pairs (integration_id must "
                            "be a valid UUID) separated by commas, or 'none'/'clear' "
                            "to clear all bindings."
                        )
                    agent.integration_bindings = _apply_bindings(
                        agent.integration_bindings, bindings
                    )
                    updated.append("integration_bindings")

            # Read-only bindings: 'provider:id' pairs whose write tools
            # are withheld and refused at run time. Follows binding_mode like
            # the binding fields, and is checked against the bindings this call
            # leaves, so a mark can never name an integration the agent does
            # not bind (where it would protect nothing).
            from app.services.integration_read_only import (
                parse_integration_read_only,
                unbound_read_only_entries,
            )

            _ro_val, _ro_err, _ro_provided = parse_integration_read_only(
                integration_read_only
            )
            if _ro_provided:
                if _ro_err:
                    return _ro_err
                if _ro_val is None and binding_op != "replace":
                    return _sentinel_mode_error("integration_read_only")
                _ro_next = (
                    _apply_bindings(agent.integration_read_only, _ro_val)
                    if _ro_val
                    else {}
                )
                _ro_unbound = unbound_read_only_entries(
                    _ro_next, agent.integration_bindings
                )
                if _ro_unbound:
                    return (
                        "integration_read_only names integrations this agent does "
                        f"not bind: {', '.join(_ro_unbound)}. Bind them first "
                        "(integration_bindings), or use the exact 'provider:id' "
                        "pairs get_agent lists."
                    )
                agent.integration_read_only = _ro_next or None
                updated.append("integration_read_only")

            # Email signature overrides: merged per binding (an entry
            # that only inherits removes the binding's override; 'clear' removes
            # them all), checked against the bindings this call leaves by the
            # rule the REST update applies.
            from app.services.email_signature_settings import (
                SignatureSettingError,
                checked_integration_signatures,
                merge_signatures,
                parse_integration_signatures,
            )

            _sig_val, _sig_err, _sig_provided = parse_integration_signatures(
                integration_signatures
            )
            if _sig_provided:
                if _sig_err:
                    return _sig_err
                try:
                    agent.integration_signatures = await checked_integration_signatures(
                        _db,
                        uid,
                        merge_signatures(agent.integration_signatures, _sig_val) if _sig_val else None,
                        bindings=agent.integration_bindings,
                        baseline=agent.integration_signatures,
                    )
                except SignatureSettingError as exc:
                    return (
                        f"{exc.detail} Use the integration ids get_agent lists for the "
                        "email-sending bindings, and a signature integration you can see."
                    )
                updated.append("integration_signatures")

            # OpenCTI Live writes overrides: merged per binding (an 'inherit'
            # entry removes the binding's override; 'clear' removes them all),
            # checked against the bindings this call leaves by the rule the
            # REST update applies (Live needs the integration's editor).
            from app.services.integration_live_writes import (
                LiveWritesSettingError,
                checked_integration_live_writes,
                merge_live_writes,
                parse_integration_live_writes,
            )

            _lw_val, _lw_err, _lw_provided = parse_integration_live_writes(
                integration_live_writes
            )
            if _lw_provided:
                if _lw_err:
                    return _lw_err
                _lw_caller = await _load_caller_user_for_builder(_db, uid)
                try:
                    agent.integration_live_writes = await checked_integration_live_writes(
                        _db,
                        uid,
                        bool(getattr(_lw_caller, "is_admin", False)),
                        merge_live_writes(agent.integration_live_writes, _lw_val) if _lw_val else None,
                        bindings=agent.integration_bindings,
                        template_bindings=agent.integration_template_bindings,
                        baseline=agent.integration_live_writes,
                    )
                except LiveWritesSettingError as exc:
                    return (
                        f"{exc.detail} Use the integration ids get_agent lists for the bindings that offer Live writes."
                    )
                updated.append("integration_live_writes")

            # ── Remaining config options (full parity with REST PUT /agents) ──
            # Every editable field on the agent is reachable here so the builder
            # can configure ANY option, not just the original handful. Each
            # field follows the same "blank = leave unchanged" contract and
            # reuses the REST validators/sanitizers so the tool can never drift
            # from the endpoint.

            # slug — validated + uniqueness-checked (mirrors REST).
            if (slug or "").strip():
                from app.core.utils import validate_slug

                try:
                    new_slug = validate_slug(slug.strip())
                except ValueError as exc:
                    return f"Invalid slug: {exc}"
                if new_slug != agent.slug:
                    exists = await _db.execute(
                        sa_select(Agent.id)
                        .where(Agent.slug == new_slug, Agent.id != agent.id)
                        .limit(1)
                    )
                    if exists.scalar_one_or_none():
                        return f"Slug '{new_slug}' is already taken."
                    agent.slug = new_slug
                    updated.append("slug")

            # guardrails — full text; "none"/"clear" resets to no guardrails.
            if (guardrails or "").strip():
                g = guardrails.strip()
                agent.guardrails = None if g.lower() in ("none", "clear") else g
                updated.append("guardrails")

            # reasoning_effort — low/medium/high/max, or none/default/inherit.
            if (reasoning_effort or "").strip():
                re_val = reasoning_effort.strip().lower()
                if re_val in ("none", "default", "inherit"):
                    agent.reasoning_effort = None
                elif re_val in ("low", "medium", "high", "max"):
                    agent.reasoning_effort = re_val
                else:
                    return (
                        "Invalid reasoning_effort: must be 'low', 'medium', "
                        "'high', 'max', or 'none' to clear."
                    )
                updated.append("reasoning_effort")

            # organization_context_mode — inherit/always/off (#11533).
            if (organization_context_mode or "").strip():
                from app.services.organization_context_injection import check_mode

                try:
                    agent.organization_context_mode = check_mode(organization_context_mode)
                except ValueError as exc:
                    return f"Invalid {exc}."
                updated.append("organization_context_mode")
            # organization_context_categories: a list, "none" (empty) or "inherit" (#11533).
            if organization_context_categories is not None and organization_context_categories != "":
                from app.services.organization_context_categories import check_categories

                try:
                    agent.organization_context_categories = check_categories(organization_context_categories)
                except ValueError as exc:
                    return f"Invalid {exc}."
                updated.append("organization_context_categories")

            # temperature — float 0.0-2.0, or none/default/inherit to clear.
            _temp_val, _temp_err, _temp_provided = _parse_optional_float_arg(
                temperature, "temperature", minimum=0.0, maximum=2.0
            )
            if _temp_provided:
                if _temp_err:
                    return _temp_err
                agent.temperature = _temp_val
                updated.append("temperature")

            # Boolean flags (true/false; blank = leave unchanged).
            _bool_fields = (
                ("skip_llm", skip_llm),
                ("disable_chat", disable_chat),
                ("allow_self_fanout", allow_self_fanout),
                ("accept_caller_output_format", accept_caller_output_format),
                ("web_enabled", web_enabled),
                ("human_in_the_loop", human_in_the_loop),
                ("memory_short_term_enabled", memory_short_term_enabled),
                ("memory_medium_term_enabled", memory_medium_term_enabled),
                ("memory_persistent_enabled", memory_persistent_enabled),
                ("memory_user_enabled", memory_user_enabled),
            )
            for _bf_name, _bf_raw in _bool_fields:
                _bv, _berr, _bprov = _parse_bool_arg(_bf_raw, _bf_name)
                if _bprov:
                    if _berr:
                        return _berr
                    setattr(agent, _bf_name, _bv)
                    updated.append(_bf_name)
            # Auto-generate a web_token only when THIS call explicitly enables
            # web exposure. REST keys the backfill off ``body.web_enabled``
            # truthiness — which is set only when the caller passes the field —
            # not off the persisted flag, so we gate on ``"web_enabled" in
            # updated``. Keying off the stored ``agent.web_enabled`` instead
            # would backfill a token onto a legacy ``web_enabled=True,
            # web_token=NULL`` row on ANY unrelated update, diverging from REST.
            if (
                "web_enabled" in updated
                and agent.web_enabled
                and not getattr(agent, "web_token", None)
            ):
                import secrets

                agent.web_token = secrets.token_urlsafe(24)

            # human_in_the_loop_prompt — free text; "none"/"clear" resets.
            if (human_in_the_loop_prompt or "").strip():
                hp = human_in_the_loop_prompt.strip()
                agent.human_in_the_loop_prompt = (
                    None if hp.lower() in ("none", "clear") else hp
                )
                updated.append("human_in_the_loop_prompt")

            # builtin_tool_ids — CSV of the supported builtin tools, or clear.
            # Follows binding_mode like a binding: 'replace' (default) makes
            # the list the set, 'add' merges it in, 'remove' takes it out (a
            # group and its [DEPRECATED] former ids compare as one), so a
            # caller adding one group never rewrites the groups it did not name.
            _builtin_change = ""
            if (builtin_tool_ids or "").strip():
                _bt_before = list(agent.builtin_tool_ids or [])
                if _is_clear_sentinel(builtin_tool_ids):
                    if binding_op != "replace":
                        return _sentinel_mode_error("the built-in tools")
                    _bt_after: list[str] = []
                else:
                    bt = _csv_str_list(builtin_tool_ids)
                    invalid_bt = [t for t in bt if t not in _VALID_BUILTIN_TOOL_IDS]
                    if invalid_bt:
                        return (
                            "Invalid builtin_tool_ids: "
                            f"{', '.join(invalid_bt)}. Supported: "
                            f"{', '.join(_advertised_builtin_tool_ids())} "
                            "(comma-separated, or 'none'/'clear')."
                        )
                    _bt_after = _apply_builtin_binding_mode(_bt_before, bt, binding_op)
                    # An administrator-only group (#11594) is bound by an administrator alone.
                    from app.services.builtin_tools.organization_context_write import refused_admin_bound_ids

                    _bt_caller = await _load_caller_user_for_builder(_db, uid)
                    _bt_refused = refused_admin_bound_ids(
                        _bt_after, before=_bt_before, caller_is_admin=bool(getattr(_bt_caller, "is_admin", False))
                    )
                    if _bt_refused:
                        return (
                            "Only an administrator can bind these built-in tools: "
                            f"{', '.join(_bt_refused)}. Nothing was changed."
                        )
                agent.builtin_tool_ids = _bt_after
                _builtin_change = _describe_builtin_change(_bt_before, _bt_after)
                updated.append("builtin_tool_ids")

            # tool_deny_list — CSV of tool names to always block, or clear.
            if (tool_deny_list or "").strip():
                from app.services.builtin_tools.scheduling import normalize_tool_deny_list

                try:
                    _denied = (
                        None
                        if tool_deny_list.strip().lower() in ("none", "clear")
                        else (normalize_tool_deny_list(_csv_str_list(tool_deny_list)) or None)
                    )
                except ValueError as exc:
                    return f"Error: {exc}"
                agent.tool_deny_list = _denied
                updated.append("tool_deny_list")

            # integration_template_bindings — CSV of provider keys, or clear.
            # Same rule as the REST validator: an OpenAPI integration is its
            # own API and never resolves per user.
            if (integration_template_bindings or "").strip():
                from app.services.openapi_import.service import (
                    per_user_binding_error,
                )

                _template_providers = (
                    []
                    if integration_template_bindings.strip().lower()
                    in ("none", "clear")
                    else [
                        p.lower()
                        for p in _csv_str_list(integration_template_bindings)
                    ]
                )
                _template_error = per_user_binding_error(_template_providers)
                if _template_error:
                    return _template_error
                agent.integration_template_bindings = _template_providers
                updated.append("integration_template_bindings")
                agent.prune_orphaned_integration_template_read_only()

            # integration_template_read_only: the template-bound providers
            # whose resolved integration is read-only. REPLACES the list and is
            # checked against the template bindings this call leaves.
            from app.services.integration_read_only import (
                parse_integration_template_read_only,
                untemplated_read_only_detail,
                untemplated_read_only_providers,
            )

            _tro_val, _tro_err, _tro_provided = parse_integration_template_read_only(
                integration_template_read_only
            )
            if _tro_provided:
                if _tro_err:
                    return _tro_err
                _tro_untemplated = untemplated_read_only_providers(
                    _tro_val, agent.integration_template_bindings
                )
                if _tro_untemplated:
                    return untemplated_read_only_detail(_tro_untemplated)
                agent.integration_template_read_only = _tro_val or None
                updated.append("integration_template_read_only")

            # web_suggested_prompts — newline-separated (prompts may contain
            # commas), or "none"/"clear".
            if (web_suggested_prompts or "").strip():
                if web_suggested_prompts.strip().lower() in ("none", "clear"):
                    agent.web_suggested_prompts = None
                else:
                    _wsp = [
                        ln.strip()
                        for ln in web_suggested_prompts.splitlines()
                        if ln.strip()
                    ]
                    agent.web_suggested_prompts = _wsp or None
                updated.append("web_suggested_prompts")

            # shared_group_ids — CSV of group UUIDs, or "none"/"clear". Stored
            # with shared_group_permissions below, through the REST writer.
            _sg_given = bool((shared_group_ids or "").strip())
            _sg_ids: list[str] | None = None
            if _sg_given and shared_group_ids.strip().lower() not in ("none", "clear"):
                sg, sg_invalid = _csv_uuids(shared_group_ids)
                if sg is None:
                    return _invalid_ids_error("shared_group_ids", sg_invalid)
                _sg_ids = sg

            # JSON-object fields — REPLACE, or "none"/"clear" to reset to None.
            # (shared_group_permissions is handled separately below because REST
            # collapses an empty dict to None, unlike these fields.)
            _json_fields = (
                ("output_schema", output_schema),
                ("config", config),
                ("prepare_tool_ref", prepare_tool_ref),
                ("finalize_tool_ref", finalize_tool_ref),
            )
            for _jf_name, _jf_raw in _json_fields:
                _jv, _jerr, _jprov = _parse_optional_json_arg(_jf_raw, _jf_name)
                if _jprov:
                    if _jerr:
                        return _jerr
                    if _jf_name in PIPELINE_TOOL_REF_FIELDS:
                        _caller = await _load_caller_user_for_builder(_db, uid)
                        _refusal = await pipeline_refs_refusal(
                            _db,
                            uid,
                            is_admin=agent_tool_admin_bypass(_caller),
                            refs=[_jv],
                            stored=[getattr(agent, _jf_name, None)],
                        )
                        if _refusal:
                            return _refusal
                    setattr(agent, _jf_name, _jv)
                    updated.append(_jf_name)

            # shared_group_permissions — per-group access map. The pair goes
            # through ``normalize_group_grants``, the REST routes' writer:
            # canonical ids, an added group must exist, a deleted one
            # is dropped, known tiers, an empty map stored as None.
            _sgp_val, _sgp_err, _sgp_prov = _parse_optional_json_arg(
                shared_group_permissions, "shared_group_permissions"
            )
            if _sgp_prov and _sgp_err:
                return _sgp_err
            if _sg_given or _sgp_prov:
                from fastapi import HTTPException

                from app.api.v1.helpers import normalize_group_grants

                try:
                    (
                        agent.shared_group_ids,
                        agent.shared_group_permissions,
                    ) = await normalize_group_grants(
                        _db,
                        _sg_ids if _sg_given else agent.shared_group_ids,
                        _sgp_val if _sgp_prov else agent.shared_group_permissions,
                        existing_ids=agent.shared_group_ids,
                    )
                except HTTPException as exc:
                    return f"Cannot share agent **{agent.name}**: {exc.detail}"
                if _sg_given:
                    updated.append("shared_group_ids")
                if _sgp_prov:
                    updated.append("shared_group_permissions")

            # output_schema must be a valid JSON Schema (mirror REST validator).
            if "output_schema" in updated and agent.output_schema is not None:
                if len(json.dumps(agent.output_schema)) > 50_000:
                    return "output_schema must be under 50 KB."
                from jsonschema import Draft7Validator, SchemaError

                try:
                    Draft7Validator.check_schema(agent.output_schema)
                except SchemaError as exc:
                    return f"Invalid output_schema (JSON Schema): {exc.message}"

            # Tool/element scoping maps (JSON, nullable). Normalize exactly like
            # REST: strip per-key null values (= "all tools for that source") and
            # collapse an empty dict to None. REST gets the per-key value shapes
            # for free from Pydantic; the builder bypasses it, so validate them
            # here too (see ``_validate_scoping_map``) — a bare-string value
            # would otherwise persist and silently corrupt runtime tool
            # filtering. The third tuple item flags the element-bindings map,
            # whose values are lists of objects rather than tool-name strings.
            _scoping_fields = (
                ("integration_allowed_tools", integration_allowed_tools, False),
                ("mcp_allowed_tools", mcp_allowed_tools, False),
                ("integration_element_bindings", integration_element_bindings, True),
            )
            for _sf_name, _sf_raw, _sf_is_bindings in _scoping_fields:
                _sv, _serr, _sprov = _parse_optional_json_arg(_sf_raw, _sf_name)
                if _sprov:
                    if _serr:
                        return _serr
                    if isinstance(_sv, dict):
                        cleaned = {k: v for k, v in _sv.items() if v is not None}
                        _shape_err = _validate_scoping_map(
                            _sf_name, cleaned, is_bindings=_sf_is_bindings
                        )
                        if _shape_err:
                            return _shape_err
                        _sv = cleaned or None
                    if _sf_name == "integration_allowed_tools":
                        # A tool its provider does not expose is refused, the
                        # closest valid names given; nothing is saved. An
                        # ``openapi`` list is checked against the bound
                        # OpenAPI integrations (bindings are applied above).
                        from app.services.openapi_import import (
                            openapi_integration_ids as _openapi_ids,
                        )
                        from app.services.tool_scope_names import (
                            IntegrationAllowedToolsError,
                            validate_integration_allowed_tools,
                        )

                        try:
                            await validate_integration_allowed_tools(_db, _sv, _openapi_ids(agent.integration_bindings))
                        except IntegrationAllowedToolsError as exc:
                            return f"Error: {exc}"
                    setattr(agent, _sf_name, _sv)
                    updated.append(_sf_name)

            # The stored allow list before this write: an entry it leaves as
            # is for a group not enabled is dropped, a new one refused (#1428).
            _ba_stored = getattr(agent, "builtin_allowed_tools", None)
            # builtin_allowed_tools: built-in group id -> function
            # names, validated against the group's functions like REST (the
            # error names the valid ones); null or {} = no scoping.
            _ba_v, _ba_err, _ba_prov = _parse_optional_json_arg(
                builtin_allowed_tools, "builtin_allowed_tools"
            )
            if _ba_prov:
                if _ba_err:
                    return _ba_err
                from app.services.builtin_allowed_tools import (
                    BuiltinAllowedToolsError,
                    normalize_builtin_allowed_tools,
                    record_person_scope,
                )

                _ba_before = agent.builtin_allowed_tools
                try:
                    agent.builtin_allowed_tools = normalize_builtin_allowed_tools(_ba_v)
                except BuiltinAllowedToolsError as exc:
                    return f"Error: {exc}"
                # A list a person asked for is no longer the platform's (#39).
                record_person_scope(agent, _ba_before)
                updated.append("builtin_allowed_tools")

            # A [DEPRECATED] built-in id is stored as its group.
            if {"builtin_tool_ids", "builtin_allowed_tools"}.intersection(updated):
                from app.services.builtin_allowed_tools import not_enabled_written_or_pruned
                from app.services.builtin_tool_aliases import canonical_builtin_selection, fold_builtin_selection

                fold_builtin_selection(agent)
                _ba_refusal = not_enabled_written_or_pruned(
                    agent, canonical_builtin_selection(list(agent.builtin_tool_ids or []), _ba_stored)[1]
                )
                if _ba_refusal:
                    return f"Error: {_ba_refusal}"

            # An allow-list under the shared ``openapi`` provider is split per
            # OpenAPI integration (mirrors REST), also when the bindings change.
            if {"integration_allowed_tools", "integration_bindings"}.intersection(updated):
                from app.services.openapi_import import (
                    openapi_integration_ids,
                    per_integration_tool_scopes,
                )

                agent.integration_allowed_tools = await per_integration_tool_scopes(
                    _db,
                    agent.integration_allowed_tools,
                    openapi_integration_ids(agent.integration_bindings),
                )

            # integration_element_enforcement — the element types whose Scope(s)
            # selection is a boundary on the tools: comma-separated (or
            # a JSON array of) element-binding keys; 'none'/'clear' makes every
            # selection guidance again. Validated like REST, and a flag is kept
            # only while its type is selected.
            if (integration_element_enforcement or "").strip():
                _ee_text = integration_element_enforcement.strip()
                if _ee_text.lower() in ("none", "clear"):
                    agent.integration_element_enforcement = None
                else:
                    if _ee_text.startswith("["):
                        try:
                            _ee_items = json.loads(_ee_text)
                        except ValueError:
                            return (
                                "integration_element_enforcement must be a "
                                "comma-separated list or a JSON array of element types."
                            )
                    else:
                        _ee_items = _csv_str_list(_ee_text)
                    try:
                        agent.integration_element_enforcement = (
                            parse_scope_enforcement(_ee_items) or None
                        )
                    except ScopeEnforcementError as exc:
                        return str(exc)
                updated.append("integration_element_enforcement")
            if {"integration_element_bindings", "integration_element_enforcement"}.intersection(
                updated
            ):
                agent.prune_orphaned_element_enforcement()

            # Prune integration scope/tool entries for providers no longer active
            # whenever a binding set changed (mirrors REST).
            if {"integration_bindings", "integration_template_bindings"}.intersection(
                updated
            ):
                agent.prune_orphaned_integration_scopes()

            # available_channel_types — admin-only; "none"/"clear" = all channels.
            if (available_channel_types or "").strip():
                caller = await _load_caller_user_for_builder(_db, uid)
                if caller is None or not caller.is_admin:
                    return (
                        "Only admins can change channel routing "
                        "(available_channel_types)."
                    )
                agent.available_channel_types = (
                    None
                    if available_channel_types.strip().lower() in ("none", "clear")
                    else _csv_str_list(available_channel_types)
                )
                updated.append("available_channel_types")

            # channel_config — JSON; "none"/"clear" = no channels. Derive
            # available_channel_types from it (mirrors REST); validated below.
            _channel_config_changed = False
            if _arg_provided(channel_config):
                from app.services.agent_bindings import derive_available_channel_types

                _cc_val, _cc_err, _cc_prov = _parse_optional_json_arg(
                    channel_config, "channel_config"
                )
                if _cc_err:
                    return _cc_err
                if _cc_prov:
                    # The builder bypasses the REST ``dict[str, dict]`` Pydantic
                    # type, so a non-object channel entry (e.g.
                    # ``{"slack": "oops"}``) would reach
                    # ``derive_available_channel_types`` (and then
                    # ``validate_channel_config``), both of which call
                    # ``ch_cfg.get(...)``, and raise an uncaught
                    # ``AttributeError`` (a 500-style tool crash) instead of a
                    # friendly, correctable error. Reject non-object values up
                    # front. JSON object keys are always strings, so only the
                    # values need a shape check.
                    if _cc_val is not None and not all(
                        v is None or isinstance(v, dict) for v in _cc_val.values()
                    ):
                        return (
                            "channel_config values must be JSON objects (a JSON "
                            "object mapping each channel type to its config "
                            "object, null to drop it), or 'none'/'clear' to reset."
                        )
                    _cc_stored = agent.channel_config
                    if _cc_val is not None:
                        # Merged into the stored map like an integration's
                        # extra_config (#9898): a channel or key left out
                        # keeps its value, null drops it. Setting one flag
                        # never drops the bot id of another channel.
                        from app.api.v1.integrations import merge_extra_config_values

                        _cc_val = (
                            merge_extra_config_values(_cc_stored or {}, _cc_val)
                            or None
                        )
                    agent.channel_config = _cc_val
                    agent.available_channel_types = derive_available_channel_types(
                        _cc_val, agent.available_channel_types
                    )
                    updated.append("channel_config")
                    _channel_config_changed = True

            # handover_types — "<agent_id>:<type>" pairs, or "none"/"clear".
            if (handover_types or "").strip():
                if handover_types.strip().lower() in ("none", "clear"):
                    agent.handover_types = {}
                else:
                    ht_map: dict[str, str] = {}
                    ht_malformed: list[str] = []
                    valid_types = (
                        "delegate",
                        "consult",
                        "transfer",
                        "mandatory_transfer",
                    )
                    for raw_pair in handover_types.split(","):
                        cleaned = raw_pair.strip()
                        if not cleaned:
                            continue
                        aid, sep, htype = cleaned.partition(":")
                        aid, htype = aid.strip(), htype.strip().lower()
                        parsed_aid = safe_uuid(aid)
                        if not sep or not parsed_aid or htype not in valid_types:
                            ht_malformed.append(cleaned)
                            continue
                        ht_map[str(parsed_aid)] = htype
                    if ht_malformed:
                        return (
                            "Invalid handover_types entry(ies): "
                            f"{', '.join(ht_malformed)}. Use 'AGENT_ID:TYPE' "
                            "pairs separated by commas (TYPE is delegate, "
                            "consult, transfer or mandatory_transfer), or "
                            "'none'/'clear' to reset."
                        )
                    agent.handover_types = ht_map
                updated.append("handover_types")

            # handover_conditions — JSON {agent_id: condition}, or "none"/"clear".
            if _arg_provided(handover_conditions):
                _hc_val, _hc_err, _hc_prov = _parse_optional_json_arg(
                    handover_conditions, "handover_conditions"
                )
                if _hc_err:
                    return _hc_err
                if _hc_prov:
                    # Mirror the REST ``dict[str, str]`` Pydantic type. The
                    # builder bypasses Pydantic, so a non-string value (e.g.
                    # ``{"<uuid>": 123}``) would otherwise reach
                    # ``validate_handover_conditions`` -> ``.strip().lower()``
                    # and raise an uncaught ``AttributeError`` (a 500-style tool
                    # crash) instead of a friendly, correctable error. JSON
                    # object keys are always strings, so only the values need a
                    # shape check here.
                    if _hc_val is not None and not all(
                        isinstance(v, str) for v in _hc_val.values()
                    ):
                        return (
                            "handover_conditions values must be strings (a JSON "
                            "object mapping each agent_id to its routing "
                            "condition text), or 'none'/'clear' to reset."
                        )
                    agent.handover_conditions = _hc_val or {}
                    updated.append("handover_conditions")

            # Keep handover maps aligned with the target list and enforce the
            # mandatory-transfer condition rules — reuse the REST sanitizers so
            # the builder can never drift from the endpoint.
            if {
                "handover_agent_ids",
                "handover_types",
                "handover_conditions",
            }.intersection(updated):
                from fastapi import HTTPException

                from app.services.agent_bindings import (
                    sanitize_handover_config,
                    validate_handover_conditions,
                )

                cleaned_ids, cleaned_types, cleaned_conditions = (
                    sanitize_handover_config(
                        agent.handover_agent_ids,
                        agent.handover_types,
                        agent.handover_conditions,
                        types_before=stored_handover_types,
                    )
                )
                allowed_ids = {str(a) for a in cleaned_ids}
                cleaned_conditions = {
                    aid: cond
                    for aid, cond in cleaned_conditions.items()
                    if aid in allowed_ids
                    and cleaned_types.get(aid) == "mandatory_transfer"
                } or {}
                try:
                    validate_handover_conditions(
                        cleaned_ids, cleaned_types, cleaned_conditions
                    )
                except HTTPException as exc:
                    return str(exc.detail)
                agent.handover_agent_ids = cleaned_ids
                agent.handover_types = cleaned_types
                agent.handover_conditions = cleaned_conditions

            # Company-managed (visibility) toggle. Parsed like ``enabled``
            # (true/false); a whitespace-only or unset value leaves it
            # unchanged. Editing is already writable-gated above, but flipping
            # platform-wide visibility is a stricter, distinct capability
            # (a non-admin owner can edit their own private agent yet must
            # never be able to publish it company-wide), so it carries its own
            # admin gate — mirroring the REST ``PUT /agents/{id}`` contract.
            if (is_company_managed or "").strip().lower() in ("true", "false"):
                want_cm = (is_company_managed or "").strip().lower() == "true"
                if want_cm != bool(agent.is_company_managed):
                    caller = await _load_caller_user_for_builder(_db, uid)
                    if caller is None or not caller.is_admin:
                        return (
                            "Only admins can change the company-managed "
                            "(platform-wide visibility) status of an agent."
                        )
                    cm_err = await _apply_agent_company_managed_toggle(
                        _db, agent, want_cm, caller.id
                    )
                    if cm_err:
                        await _db.rollback()
                        return cm_err
                    updated.append("company-managed")

            # Public catalog publishing — only company-managed agents may be
            # published, and an admin gate applies (mirrors REST). Applied AFTER
            # the company-managed toggle so an agent made company-managed in the
            # same call can be published in one shot.
            _ipc_val, _ipc_err, _ipc_prov = _parse_bool_arg(
                is_public_catalog, "is_public_catalog"
            )
            if _ipc_prov:
                if _ipc_err:
                    return _ipc_err
                if _ipc_val:
                    if not agent.is_company_managed:
                        return (
                            "Only company-managed agents can be published to the "
                            "public catalog. Set is_company_managed=true first."
                        )
                    caller = await _load_caller_user_for_builder(_db, uid)
                    if caller is None or not caller.is_admin:
                        return (
                            "Only admins can publish an agent to the public catalog."
                        )
                agent.is_public_catalog = _ipc_val
                updated.append("is_public_catalog")

            # What the catalog download carries: the REST switch and its
            # gate - on a company-managed agent only an admin may change it.
            _cipt_val, _cipt_err, _cipt_prov = _parse_bool_arg(
                catalog_include_personal_tools, "catalog_include_personal_tools"
            )
            _cipt_caller = None
            if _cipt_prov:
                if _cipt_err:
                    return _cipt_err
                _cipt_caller = await _load_caller_user_for_builder(_db, uid)
                if _cipt_caller is None or (
                    agent.is_company_managed and not _cipt_caller.is_admin
                ):
                    return (
                        "Only admins can change the catalog settings of "
                        "company-managed entities."
                    )
                agent.catalog_include_personal_tools = _cipt_val
                updated.append("catalog_include_personal_tools")

            # Per-user sharing: the people ``users`` names, by email or
            # user id, resolved to EXISTING accounts (``share_principals``, the reading
            # the share_presentation tool uses) and written through the one
            # grant write every surface shares (``apply_user_share_fields`` ->
            # ``normalize_user_grants``: canonical ids, active accounts for a
            # new grant, the accounts locked). After the company-managed toggle,
            # as on the REST update: a grant is only meaningful relative to it.
            # ``binding_mode`` decides how the list applies, like a binding.
            _share_lines: list[str] = []
            _share_audit: dict[str, Any] | None = None
            _users_given = _arg_provided(users)
            _tier_given = _arg_provided(users_permission)
            if _users_given or _tier_given:
                from fastapi import HTTPException

                from app.api.v1.helpers import apply_user_share_fields
                from app.services.share_principals import (
                    MAX_PRINCIPALS_PER_CALL,
                    PrincipalIndex,
                    SharingChange,
                    plan_grants,
                    principal_entries,
                    resolve_principals,
                    stored_grants,
                )

                if not _users_given:
                    return (
                        "users_permission names the access given to the people in "
                        "`users`; pass `users` too (emails or user ids)."
                    )
                _share_tier = _agent_share_tier(users_permission) if _tier_given else "read_only"
                if _share_tier is None:
                    return (
                        "Invalid users_permission: use 'read_only' (see and chat with "
                        "the agent), 'config' (also edit it) or 'config_with_chats' "
                        "(also read its chats)."
                    )
                _clear_users = isinstance(users, str) and _is_clear_sentinel(users)
                if _clear_users and binding_op != "replace":
                    return _sentinel_mode_error("the per-user shares")
                _entries = [] if _clear_users else principal_entries(users)
                if not _entries and not _clear_users:
                    return (
                        "No valid users found in the input — provide one or more "
                        "email addresses or user ids (a list, or comma-separated), "
                        "or 'none'/'clear' to remove every per-user share."
                    )
                if len(_entries) > MAX_PRINCIPALS_PER_CALL:
                    return (
                        f"One call names at most {MAX_PRINCIPALS_PER_CALL} people - "
                        "share with a group instead, or split the list."
                    )
                if agent.is_company_managed and not agent.access_restricted:
                    return (
                        f"Agent **{agent.name}** is company-managed: everyone on the "
                        "platform can already use it, so it is not shared with "
                        "individual people. Per-user sharing applies to a private "
                        "agent, or to a company-managed agent an admin restricted to "
                        "named groups and people from its Configuration page. Nothing "
                        "was changed."
                    )
                _resolved = await resolve_principals(_db, _entries)
                _refusal = _resolved.refusal(granting=binding_op != "remove")
                if _refusal:
                    return f"Cannot share agent **{agent.name}**: {_refusal}"
                # The owner reaches their own agent unconditionally: named, they
                # are reported, never stored (``normalize_user_grants`` drops them).
                _owner_id = str(agent.user_id) if agent.user_id else None
                _owned = [p for p in _resolved.users if p.id == _owner_id]
                _people = [p for p in _resolved.users if p.id != _owner_id]
                _index = PrincipalIndex()
                _index.add(_resolved.users)
                _current = stored_grants(agent.shared_user_ids, agent.shared_user_permissions)
                _new_ids, _new_tiers, _delta = plan_grants(binding_op, _share_tier, _current, _people)
                if _delta.changed:
                    try:
                        await apply_user_share_fields(
                            _db,
                            agent,
                            SharingChange(
                                shared_user_ids=_new_ids or None,
                                shared_user_permissions=_new_tiers or None,
                            ),
                            partial=True,
                        )
                    except HTTPException as exc:
                        await _db.rollback()
                        return f"Cannot share agent **{agent.name}**: {exc.detail}"
                    updated.append("users")
                    _share_audit = {
                        "via": "agent",
                        "sharing_changed": True,
                        "action": binding_op,
                        "permission": None if binding_op == "remove" else _share_tier,
                        "added_users": _delta.added,
                        "removed_users": _delta.removed,
                        "retiered_users": _delta.retiered,
                    }

                def _named(ids: list[str]) -> str:
                    return ", ".join(_index.user(key).label for key in ids)

                if _delta.added:
                    _share_lines.append(f"Shared with {_named(_delta.added)} [{_share_tier}].")
                if _delta.retiered:
                    _share_lines.append(f"Now {_share_tier}: {_named(_delta.retiered)}.")
                if _delta.removed:
                    _share_lines.append(
                        f"Access removed, they can no longer use it: {_named(_delta.removed)}."
                    )
                if _delta.unchanged:
                    _share_lines.append(f"Already {_share_tier}, unchanged: {_named(_delta.unchanged)}.")
                if _delta.not_shared:
                    _share_lines.append(f"Not shared with them, nothing to remove: {_named(_delta.not_shared)}.")
                if _owned:
                    _share_lines.append(f"Skipped, they own it: {', '.join(p.label for p in _owned)}.")
                if not _delta.changed and not updated:
                    return f"Nothing changed on agent **{agent.name}**: " + " ".join(_share_lines)

            if not updated:
                return "No fields to update. Provide at least one field."

            # Mirror the REST ``PUT /agents/{id}`` contract: a company-managed
            # agent may only bind company-managed MCP servers, KBs, integrations,
            # and handover agents. The toggle helper above already strips +
            # validates on the private -> company-managed flip; this guards the
            # other path REST also covers — editing the bindings of an *already*
            # company-managed agent, which would otherwise let an admin attach
            # personal resources to a shared agent. Only the binding fields that
            # ``validate_company_managed_bindings`` checks need to (re)trigger
            # it (an unrelated edit such as a rename can't make a binding
            # non-compliant). Roll back on failure so no partial change is
            # persisted, returning the same friendly detail as REST.
            _cm_validated_binding_fields = {
                "knowledge_base_ids",
                "mcp_server_ids",
                "integration_bindings",
                "handover_agent_ids",
            }
            if _cm_validated_binding_fields.intersection(
                updated
            ) and agent.is_company_managed:
                from fastapi import HTTPException

                from app.services.agent_bindings import validate_company_managed_bindings

                try:
                    await validate_company_managed_bindings(
                        _db,
                        mcp_server_ids=agent.mcp_server_ids,
                        knowledge_base_ids=agent.knowledge_base_ids,
                        integration_bindings=agent.integration_bindings,
                        handover_agent_ids=agent.handover_agent_ids,
                    )
                except HTTPException as exc:
                    await _db.rollback()
                    return str(exc.detail)

            # Mirror the REST ``PUT /agents/{id}`` personal-binding visibility
            # gate (``agents.py::validate_personal_agent_bindings``, called for
            # non-company-managed agents). Without it the builder only checked
            # that the AGENT was writable — never that the bound resources were
            # VISIBLE to the caller — so a non-admin could bind another user's
            # private integration (hijacking its OAuth token / API key), KB, MCP
            # server, custom tool, handover agent, skill, or prompt by guessing a
            # UUID. Only the binding fields actually changed in this call are
            # checked (mirrors REST's ``model_fields_set`` gating). Pure removals
            # are skipped: ``remove`` mode can only subtract, so it can never
            # grant access to a resource the caller couldn't already reach.
            # Admins bypass the visibility check, exactly like REST, unless
            # AGENT_TOOLS_STRICT_ADMIN_ACCESS.
            _personal_binding_fields = {
                "knowledge_base_ids",
                "mcp_server_ids",
                "integration_bindings",
                "handover_agent_ids",
                "custom_tool_ids",
                "skill_ids",
                "prompt_ids",
            }
            if (
                not agent.is_company_managed
                and binding_op != "remove"
                and _personal_binding_fields.intersection(updated)
            ):
                from fastapi import HTTPException

                from app.api.v1.helpers import get_user_group_ids
                from app.services.agent_bindings import validate_personal_agent_bindings

                _pb_caller = await _load_caller_user_for_builder(_db, uid)
                _pb_group_ids = await get_user_group_ids(_db, uid)
                try:
                    await validate_personal_agent_bindings(
                        _db,
                        uid,
                        _pb_group_ids,
                        is_admin=agent_tool_admin_bypass(_pb_caller),
                        mcp_server_ids=(
                            agent.mcp_server_ids
                            if "mcp_server_ids" in updated
                            else None
                        ),
                        knowledge_base_ids=(
                            agent.knowledge_base_ids
                            if "knowledge_base_ids" in updated
                            else None
                        ),
                        # Only an id this call adds; the merged map also
                        # holds bindings someone else set earlier.
                        integration_bindings=(
                            added_integration_bindings(
                                agent.integration_bindings, _stored_integration_ids
                            )
                            if "integration_bindings" in updated
                            else None
                        ),
                        handover_agent_ids=(
                            agent.handover_agent_ids
                            if "handover_agent_ids" in updated
                            else None
                        ),
                        custom_tool_ids=(
                            agent.custom_tool_ids
                            if "custom_tool_ids" in updated
                            else None
                        ),
                        skill_ids=(
                            agent.skill_ids if "skill_ids" in updated else None
                        ),
                        prompt_ids=(
                            agent.prompt_ids if "prompt_ids" in updated else None
                        ),
                    )
                except HTTPException as exc:
                    await _db.rollback()
                    return str(exc.detail)

            # Validate channel_config against the bot whitelists (reuse the REST
            # validator). Done after the company-managed toggle so it sees the
            # final visibility, mirroring the endpoint. Roll back on failure so
            # no partial change is persisted.
            if _channel_config_changed:
                from types import SimpleNamespace

                from fastapi import HTTPException

                from app.api.v1.binding_access import (
                    assert_channel_bot_additions_allowed,
                )
                from app.services.agent_bindings import validate_channel_config

                _cc_caller = await _load_caller_user_for_builder(_db, uid)
                try:
                    # Only a bot this call ADDS must be one the caller can see
                    # (the REST update rule, with the agent tools' admin
                    # bypass); checked first so a missing bot is refused
                    # exactly like a hidden one.
                    await assert_channel_bot_additions_allowed(
                        _db,
                        user=_cc_caller or SimpleNamespace(id=uid, is_admin=False),
                        channel_config=agent.channel_config,
                        baseline=_cc_stored,
                        is_admin=agent_tool_admin_bypass(_cc_caller),
                    )
                    await validate_channel_config(
                        _db,
                        channel_config=agent.channel_config,
                        is_company_managed=agent.is_company_managed,
                        is_admin=bool(_cc_caller and _cc_caller.is_admin),
                    )
                except HTTPException as exc:
                    await _db.rollback()
                    return str(exc.detail)

            # Mirror the REST endpoint's skill-package reconciliation: a skill
            # that ships ``scripts/`` or ``references/`` carries the Custom Tools
            # and KBs the import materialized from them onto every agent it is
            # bound to, and takes them away when detached. Runs after the binding
            # validators for the same reason as REST (the derived entities are
            # owned by the skill, not the caller).
            _skill_pkg_changes = None
            if {"skill_ids", "knowledge_base_ids", "custom_tool_ids"}.intersection(
                updated
            ):
                from app.services.skill_package_bindings import (
                    reconcile_agent_skill_package_bindings,
                )

                _skill_pkg_changes = await reconcile_agent_skill_package_bindings(
                    _db, agent
                )

            # Every value that changed is versioned, whatever label ``updated``
            # shows it under: the before/after comparison decides.
            _configuring_new = _CONFIGURING_NEW_AGENT.get()
            try:
                _persona_version, _config_versions = await record_agent_version_changes(
                    _db,
                    agent=agent,
                    before=versioned_before,
                    author_id=uid,
                    commit_message=(INITIAL_COMMIT_MESSAGE if _configuring_new else BUILDER_EDIT_COMMIT_MESSAGE),
                    record_baseline=not _configuring_new,
                )
            except AgentVersionLockBusyError as exc:
                # Another transaction holds the agent row past the bounded
                # wait: nothing is saved, the caller's run goes on and retries.
                await _db.rollback()
                return f"Error: {exc.detail}"
            # A new agent's creation is already one audit entry: the versions
            # create_agent configures are not a second, separate edit.
            _version_audit: dict[str, Any] = {}
            if _persona_version is not None and not _configuring_new:
                _version_audit["persona_version"] = _persona_version.version_number
            if _config_versions and not _configuring_new:
                _version_audit["config_field_versions"] = _config_versions

            # The entry the REST update writes on every edit, naming the fields
            # changed, the switch and the versions it recorded: an unversioned
            # edit (description, bindings, flags) is audited too. A per-user
            # share change has its own sharing row below, never counted twice.
            _audit_fields = (
                []
                if _configuring_new
                else sorted({label for label in updated if not (label == "users" and _share_audit)})
            )
            _update_caller = _cipt_caller
            if _update_caller is None and (_version_audit or _audit_fields):
                _update_caller = await _load_caller_user_for_builder(_db, uid)
            if _update_caller is not None and (_cipt_caller is not None or _version_audit or _audit_fields):
                from app.services.audit import log_audit

                await log_audit(
                    _db,
                    user=_update_caller,
                    action="update",
                    entity_type="agent",
                    entity_id=getattr(agent, "id", None),
                    entity_name=agent.name,
                    summary=f"Updated agent '{agent.name}'",
                    details={
                        "slug": getattr(agent, "slug", None),
                        **(
                            {"catalog_include_personal_tools": _cipt_val}
                            if _cipt_caller is not None
                            else {}
                        ),
                        **_version_audit,
                        **({"fields": _audit_fields} if _audit_fields else {}),
                        "via": "agent",
                    },
                )

            # An access change is one audit row, as the Share dialog's and the
            # share_presentation tool's are.
            if _share_audit is not None:
                from app.services.audit import log_audit

                _share_actor = _update_caller or await _load_caller_user_for_builder(_db, uid)
                if _share_actor is not None:
                    await log_audit(
                        _db,
                        user=_share_actor,
                        action="update",
                        entity_type="agent",
                        entity_id=agent.id,
                        entity_name=agent.name,
                        summary=f"Changed who can use agent '{agent.name}' through an agent",
                        details={"slug": agent.slug, **_share_audit},
                    )
            _audience = await _agent_audience_lines(_db, agent) if _share_lines else []

            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "update_agent commit failed for agent %s by user %s",
                    agent_id,
                    user_id,
                )
                await _db.rollback()
                return f"Failed to update agent `{agent_id}`. Please try again."
            _result = f"Agent **{agent.name}** updated: {', '.join(updated)}."
            if _builtin_change:
                _result += f" {_builtin_change}"
            if text_read_back:
                _result += f"\n{text_read_back}"
            if _share_lines:
                _result += (
                    " "
                    + " ".join(_share_lines)
                    + " This applies now: a person given access finds the agent in "
                    "their agent list and can chat with it; one removed cannot "
                    "open it any more.\n"
                    + "\n".join(_audience)
                )
            # A skip is reported even when nothing else changed: silently dropping
            # a skill's knowledge base is exactly the surprise the caller needs to
            # hear about.
            if _skill_pkg_changes and (
                _skill_pkg_changes.changed
                or _skill_pkg_changes.skipped_knowledge_base_ids
            ):

                def _plural(ids: list[str], label: str) -> str:
                    return f"{len(ids)} {label}{'' if len(ids) == 1 else 's'}"

                _derived = []
                for _sign, _ids, _label in (
                    ("+", _skill_pkg_changes.added_knowledge_base_ids, "KB"),
                    ("+", _skill_pkg_changes.added_custom_tool_ids, "custom tool"),
                    ("-", _skill_pkg_changes.removed_knowledge_base_ids, "KB"),
                    ("-", _skill_pkg_changes.removed_custom_tool_ids, "custom tool"),
                ):
                    if _ids:
                        _derived.append(f"{_sign}{_plural(_ids, _label)}")
                if _derived:
                    _result += (
                        " Skill-package resources followed their skills: "
                        + ", ".join(_derived)
                        + "."
                    )
                if _skill_pkg_changes.skipped_knowledge_base_ids:
                    _result += (
                        " Not bound: "
                        + _plural(
                            _skill_pkg_changes.skipped_knowledge_base_ids, "packaged KB"
                        )
                        + " — a company-managed agent can only bind company-managed "
                        "knowledge bases."
                    )
            return _AgentUpdated(_result, fields=tuple(updated))

    # ── delete_agent ──────────────────────────────────────────────────

    async def _delete_agent(agent_id: str, successor_agent_id: str = "") -> str:
        from app.services import agent_deletion

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Agent deletion is not available for anonymous users."

        agent_uuid, id_err = _validate_id_arg(agent_id, "agent_id")
        if id_err:
            return id_err
        successor_uuid = None
        if (successor_agent_id or "").strip():
            successor_uuid, id_err = _validate_id_arg(successor_agent_id, "successor_agent_id")
            if id_err:
                return id_err

        agent_name = ""

        async def _authorize(_db: AsyncSession):
            # Same gate as the REST API's ``delete_agent``: built-in agents
            # can never be deleted; company-managed agents require admin;
            # group-shared agents require ``config`` permission.  Re-checked
            # on each attempt so a retry runs against a clean session.
            nonlocal agent_name
            agent, perm_err = await _load_writable_agent_for_builder(
                _db, uid, agent_uuid, action="delete"
            )
            if perm_err is not None or agent is None:
                return perm_err or (
                    f"Agent `{agent_id}` not found or you don't have "
                    "permission to delete it."
                )
            agent_name = agent.name
            caller = await _load_caller_user_for_builder(_db, uid)
            return caller if caller is not None else "User not found."

        # The deletion ``DELETE /agents/{id}`` runs too: serialized and retried,
        # since the LLM commonly emits several ``delete_agent`` calls at once and
        # the agent loop runs them in parallel.
        try:
            refusal = await agent_deletion.delete_agent(
                agent_uuid, authorize=_authorize, via="agent", successor_id=successor_uuid
            )
        except Exception:
            # Server-side detail (with stack trace) goes to the logs for triage;
            # the tool caller gets a generic retry message — raw exception
            # strings can leak DB constraint names, pool/connection hints, or
            # other internal schema details we don't want to expose to the LLM.
            logger.exception(
                "delete_agent failed for agent %s by user %s", agent_id, user_id
            )
            return f"Failed to delete agent `{agent_id}`. Please try again."
        if refusal is not None:
            return refusal
        if successor_uuid is not None:
            return _AgentDeleted(
                f"Agent **{agent_name}** and all its assignments have been deleted. "
                f"Its conversations continue with agent `{successor_uuid}` for the people "
                "who can use it; the others keep their history and can be continued "
                "with another agent."
            )
        return _AgentDeleted(
            f"Agent **{agent_name}** and all its assignments "
            "have been deleted."
        )

    # ── list_assignments ──────────────────────────────────────────────

    async def _list_assignments(agent_id: str = "") -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import assignment_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.assignment import Assignment

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        # ``agent_id`` is optional here ("" lists across all visible agents),
        # so only validate when the LLM actually supplied a value. An id
        # prefix of a visible agent is taken for its full id, as get_agent does.
        agent_uuid = None
        if agent_id:
            agent_uuid, id_err = await _id_arg(agent_id, "agent_id")
            if id_err:
                return id_err

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            caller = await _load_caller_user_for_builder(_db, uid) if uid else None

            # The REST list's visibility: the assignments whose agent the
            # caller can see, every assignment for an admin (unless
            # AGENT_TOOLS_STRICT_ADMIN_ACCESS).
            q = select(Assignment).where(
                assignment_visible_filter(
                    uid, group_ids, is_admin=agent_tool_admin_bypass(caller)
                )
            )
            if agent_uuid is not None:
                q = q.where(Assignment.agent_id == agent_uuid)
            result = await _db.execute(q.order_by(Assignment.name))
            assignments = result.scalars().all()

            if not assignments:
                if agent_id:
                    return (
                        f"No assignments found for agent `{agent_id}` (it may "
                        "not exist or not be visible to you)."
                    )
                return "No assignments found."

            from app.services.assignment_pause import paused_entity_ids

            owner_paused = await paused_entity_ids(_db, assignments)
            parts: list[str] = [f"Found {len(assignments)} assignment(s):\n"]
            for w in assignments:
                enabled = "enabled" if w.enabled else "disabled"
                if w.id in owner_paused:
                    enabled += ", paused: its owner's account is deactivated"
                parts.append(
                    f"• **{w.name}** (id: `{w.id}`, trigger: {w.trigger_type}, "
                    f"{enabled})\n  Agent: `{w.agent_id}`"
                )
            return "\n".join(parts)

    # ── get_assignment ────────────────────────────────────────────────

    async def _get_assignment(assignment_id: str, sections: list[str] | str | None = None) -> str:
        import json as json_mod

        from sqlalchemy import select

        from app.api.v1.helpers import find_user_assignment, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent

        assignment_uuid, id_err = await _id_arg(assignment_id, "assignment_id")
        if id_err:
            return id_err
        # An unknown section is refused before any query (#283).
        wanted_sections, sections_err = parse_get_assignment_sections(sections)
        if sections_err:
            return sections_err

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            caller = await _load_caller_user_for_builder(_db, uid) if uid else None

            # Existence and visibility in ONE lookup: an assignment the caller
            # cannot see must answer exactly like an unknown id. An
            # admin reaches every assignment, as on the REST detail route
            # (unless AGENT_TOOLS_STRICT_ADMIN_ACCESS).
            assignment = await find_user_assignment(
                _db,
                uid,
                assignment_uuid,
                group_ids,
                is_admin=agent_tool_admin_bypass(caller),
            )
            agent = None
            if assignment is not None:
                agent_result = await _db.execute(
                    select(Agent).where(Agent.id == assignment.agent_id)
                )
                agent = agent_result.scalar_one_or_none()
            if assignment is None or agent is None:
                return (
                    f"Assignment `{assignment_id}` not found or you don't " "have access."
                )

            # Mirror the REST API's ``_visible_trigger_config`` contract via
            # the shared ``webhook_secret_visible`` helper: the
            # ``webhook_secret`` is a delivery credential, visible only to
            # callers who can MANAGE the assignment (admin, owner, or a
            # ``config``-tier grantee of the parent agent — ``read_only``
            # grantees and non-admin viewers of company-managed assignments
            # get it redacted), or the tool becomes a redaction bypass.
            visible_config = assignment.trigger_config
            if isinstance(visible_config, dict) and visible_config.get(
                "webhook_secret"
            ):
                from app.api.v1.helpers import webhook_secret_visible

                if caller is None or not webhook_secret_visible(
                    caller, assignment, agent, group_ids
                ):
                    from app.core.utils import strip_webhook_secret

                    visible_config = strip_webhook_secret(visible_config)

            trig_cfg = (
                json_mod.dumps(visible_config) if visible_config else "{}"
            )

            from app.services.assignment_pause import (
                OWNER_DEACTIVATED_MESSAGE,
                is_paused_by_deactivated_owner,
            )

            # No query when the caller owns it: a signed-in account is an active
            # one. ``getattr`` guard for partial test doubles, like the fields
            # below: a real row always carries both columns the rule reads.
            owner_paused = (
                getattr(assignment, "is_company_managed", None) is not None
                and getattr(assignment, "user_id", None) not in (None, uid)
                and await is_paused_by_deactivated_owner(_db, assignment)
            )

            # HITL is tri-state: True / False / None (inherit from the agent).
            # ``getattr`` defaults keep the read tool working against partial
            # test doubles; a real Assignment row always carries these columns.
            hitl = getattr(assignment, "human_in_the_loop", None)
            hitl_display = (
                "Inherit from agent" if hitl is None else ("Yes" if hitl else "No")
            )

            def _yn(field: str, default: bool = False) -> str:
                return "Yes" if getattr(assignment, field, default) else "No"

            # Resolve the inherit-able max_iterations for display: an explicit
            # assignment value overrides the agent; NULL inherits the agent's
            # override, then the platform default. ``getattr`` defaults keep the
            # read tool resilient against partial test doubles / partial rows.
            _assignment_mi = getattr(assignment, "max_iterations", None)
            _agent_mi = getattr(agent, "max_iterations", None)
            if _assignment_mi is not None:
                _assignment_max_iterations_display = str(_assignment_mi)
            elif _agent_mi is not None:
                _assignment_max_iterations_display = (
                    f"Inherited from agent ({_agent_mi})"
                )
            else:
                _assignment_max_iterations_display = "Inherited (platform default)"

            # The organization summary this assignment's runs carry (#11533).
            from app.services.organization_context_injection import effective_for

            _org_context_on = await effective_for(_db, agent=agent, assignment=assignment)

            parts = [
                f"**{assignment.name}**",
                f"  • ID: `{assignment.id}`",
                f"  • Agent: {agent.name} (`{assignment.agent_id}`)",
                f"  • Description: {assignment.description or 'None'}",
                f"  • Trigger: {assignment.trigger_type}",
                f"  • Trigger config: {trig_cfg}",
                f"  • Delay: {assignment.delay or 'None'}",
                f"  • Enabled: {'Yes' if assignment.enabled else 'No'}"
                + (f" ({OWNER_DEACTIVATED_MESSAGE})" if owner_paused else ""),
                f"  • HITL: {hitl_display}",
                f"  • AI label: {_yn('ai_label', True)}",
                f"  • Notify on complete: {_yn('notify_on_complete')}",
                f"  • Show in digest: {_yn('show_in_digest')}",
                f"  • Dry run: {_yn('dry_run')}",
                f"  • Ignore self-triggered events: "
                f"{_yn('ignore_self_triggered_events')}",
                f"  • Retry on failure: {_yn('retry_on_failure')}",
                f"  • Max retries: {getattr(assignment, 'max_retries', 3)}",
                # max_iterations is inherit-able: NULL falls back to the agent's
                # override, then the platform default. Surface which is in effect
                # so the builder can reason about (and re-edit) it correctly.
                f"  • Max iterations: {_assignment_max_iterations_display}",
                f"  • Timeout (seconds): "
                f"{getattr(assignment, 'timeout_seconds', 300)}",
                f"  • Organization context: "
                f"{getattr(assignment, 'organization_context_mode', None) or 'inherit'}"
                f" (effective: {'on' if _org_context_on else 'off'})",
                "  • Organization categories preloaded: "
                + (
                    "inherit"
                    if getattr(assignment, "organization_context_categories", None) is None
                    else (", ".join(assignment.organization_context_categories) or "none")
                ),
            ]

            # Schedules: the next fire in the cron's zone and in UTC, computed
            # by the scheduler's own ``next_cron_run``, and the warning when
            # ``trigger_config.timezone`` is not a zone (read as UTC).
            if assignment.trigger_type == "schedule":
                from app.core.utils import describe_schedule_next_run, schedule_base

                _sched = describe_schedule_next_run(
                    assignment.trigger_config,
                    schedule_base(assignment),
                )
                if _sched["next_run_at"]:
                    parts.append(
                        f"  • Next run: {_sched['next_run_local']} ({_sched['timezone']}), "
                        f"{_sched['next_run_at']} UTC"
                    )
                else:
                    parts.append("  • Next run: none (cron not parseable)")
                if _sched["timezone_warning"]:
                    parts.append(f"  • Timezone: {_sched['timezone_warning']}")

            # Integration events: how they arrive, when last, and how many the
            # trigger's filter dropped before any run.
            if assignment.trigger_type == "integration_event":
                from app.core.redis import redis_client
                from app.services.graph_event_subscriptions import delivery_mode

                try:
                    delivery = await delivery_mode(await redis_client(), assignment)
                except Exception as exc:
                    logger.debug("get_assignment could not read the delivery mode: %r", exc)
                    delivery = {}
                if delivery.get("mode") == "subscription":
                    parts.append(
                        f"  • Delivery: Graph subscription ({delivery.get('subscriptions', 0)}), "
                        f"renewed before {delivery.get('expires_at', 'its expiry')}"
                    )
                elif delivery.get("mode") == "webhook":
                    parts.append("  • Delivery: webhook (provider URL)")
                elif delivery:
                    reason = f" - {delivery['reason']}" if delivery.get("reason") else ""
                    parts.append(
                        f"  • Delivery: poll every {delivery.get('interval_seconds', 300) // 60} min{reason}"
                    )
                if delivery.get("mode") != "webhook":
                    parts.append(f"  • Last poll: {delivery.get('last_poll_at') or 'never'}")
                    if delivery.get("poll_skipped"):
                        parts.append(f"  • Poll skipped: {delivery['poll_skipped']}")
                parts.append(f"  • Last notification: {delivery.get('last_notification_at') or 'never'}")
                _filtered = getattr(assignment, "events_filtered_count", None)
                parts.append(f"  • Events filtered: {_filtered if isinstance(_filtered, int) else 0}")
                # Why, over the last week: reason -> count, top 10.
                from app.services.integration_event_filters import filtered_reasons

                _reasons = await filtered_reasons(assignment.id)
                if _reasons:
                    parts.append("  • Dropped by reason (last 7 days):")
                    for _reason, _count in list(_reasons.items())[:10]:
                        parts.append(f"    - {_count} x {_reason}")

            # Surface the prompts in FULL (never truncated) so the agent can
            # faithfully inspect and edit them. The activation gate prompt is a
            # distinct field from the action prompt — both must be visible or
            # the builder cannot reason about the gate at all (it would
            # otherwise assume the single ``prompt`` field is the gate).
            _check = getattr(assignment, "activation_check", None)
            parts.append("")
            parts.append(GET_ASSIGNMENT_SECTIONS["gates"][0])
            parts.append(
                "  1. Tool check (no model call): "
                + (_describe_gate_check(_check) if isinstance(_check, dict) and _check else "None")
            )
            parts.append(
                "  2. Prompt gate (one model call): "
                + ("set, below" if (assignment.activation_prompt or "").strip() else "None")
            )
            parts.append("")
            parts.append(GET_ASSIGNMENT_SECTIONS["activation_prompt"][0])
            parts.append(assignment.activation_prompt or "None (always runs)")
            parts.append("")
            parts.append(GET_ASSIGNMENT_SECTIONS["prompt"][0])
            # ``prompt`` is NOT NULL, so the only falsy value is an empty
            # string; render that as "(empty)" rather than "None" so the read
            # tool does not misrepresent a persisted empty action prompt.
            parts.append(assignment.prompt if assignment.prompt else "(empty)")

            # Optional per-assignment prompt overrides (NULL = platform default).
            if assignment.ai_label_prompt:
                parts.append("")
                parts.append(GET_ASSIGNMENT_SECTIONS["ai_label_prompt"][0])
                parts.append(assignment.ai_label_prompt)
            if assignment.human_in_the_loop_prompt:
                parts.append("")
                parts.append(GET_ASSIGNMENT_SECTIONS["human_in_the_loop_prompt"][0])
                parts.append(assignment.human_in_the_loop_prompt)

            return "\n".join(project_answer_sections(parts, wanted_sections, GET_ASSIGNMENT_SECTIONS))

    # ── update_assignment ─────────────────────────────────────────────

    async def _update_assignment(
        assignment_id: str,
        name: str = "",
        prompt: str = "",
        activation_prompt: str = "",
        trigger_type: str = "",
        trigger_config: str = "",
        delay: str = "",
        enabled: str = "",
        description: str = "",
        ai_label_prompt: str = "",
        human_in_the_loop_prompt: str = "",
        human_in_the_loop: str = "",
        ai_label: str = "",
        notify_on_complete: str = "",
        show_in_digest: str = "",
        dry_run: str = "",
        ignore_self_triggered_events: str = "",
        retry_on_failure: str = "",
        max_retries: str = "",
        max_iterations: str = "",
        timeout_seconds: str = "",
        activation_check: str | dict | None = "",
        wait_policy: object = "",
        organization_context_mode: str = "",
        organization_context_categories: object = None,
        mode: str = "replace",
        field: str = "",
        edits: list[dict[str, object]] | None = None,
        section: str = "",
        content: str = "",
        fix_applied: str = "",
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.models.assignment import assignment_text_length_error
        from app.services.assignment_activation_check import parse_activation_check
        from app.services.skill_edits import SkillEditError, partial_mode_args, partial_text_update

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Assignment updates are not available for anonymous users."
        check_value, check_err, check_provided = parse_activation_check(activation_check)
        if check_err is not None:
            return f"{check_err} Nothing was saved."

        assignment_uuid, id_err = await _id_arg(assignment_id, "assignment_id")
        if id_err:
            return id_err

        # A partial mode edits one stored text field (find/replace, append, one
        # section) instead of resending it whole: the helper update_skill uses.
        try:
            text_mode, text_field = partial_mode_args(
                mode,
                field,
                fields=ASSIGNMENT_PARTIAL_TEXT_FIELDS,
                whole={
                    "prompt": prompt,
                    "activation_prompt": activation_prompt,
                    "description": description,
                },
                content=content,
            )
        except SkillEditError as exc:
            return f"Error: {exc} Nothing was saved."

        # Type-guard the string fields before any .strip()/.lower(): tool kwargs
        # are forwarded verbatim, so a native JSON non-string (e.g. true / 1)
        # would otherwise crash with AttributeError. The bool / int fields are
        # parsed separately (their parsers already handle non-strings). None is
        # allowed (treated as "leave unchanged").
        for _str_label, _str_value in (
            ("name", name),
            ("prompt", prompt),
            ("activation_prompt", activation_prompt),
            ("trigger_type", trigger_type),
            ("trigger_config", trigger_config),
            ("delay", delay),
            ("description", description),
            ("ai_label_prompt", ai_label_prompt),
            ("human_in_the_loop_prompt", human_in_the_loop_prompt),
        ):
            if _str_value is not None and not isinstance(_str_value, str):
                return f"{_str_label} must be a string."
            _surrogate_err = lone_surrogate_error(_str_label, _str_value)
            if _surrogate_err is not None:
                return f"{_surrogate_err} Nothing was saved."

        # Measured as stored, stripped. ``trigger_type`` needs no check: the
        # allow-list below admits only types shorter than its column.
        for _len_field, _len_value in (
            ("name", (name or "").strip()),
            ("delay", (delay or "").strip()),
        ):
            _len_err = assignment_text_length_error(_len_field, _len_value)
            if _len_err is not None:
                return _len_err

        async with _builder_session() as _db:
            # Same gate as the REST API's ``update_assignment``:
            # built-in parent agents are read-only, company-managed
            # assignments (user_id IS NULL) require admin, group-shared
            # parents require ``config`` permission. The previous
            # ``Assignment.user_id == uid`` filter blocked admins from
            # editing company-managed assignments through the tool even
            # though the UI allowed it.
            assignment, _agent, perm_err = await _load_writable_assignment_for_builder(
                _db, uid, assignment_uuid, action="edit"
            )
            if perm_err is not None or assignment is None:
                return perm_err or (
                    f"Assignment `{assignment_id}` not found or you don't have "
                    "permission to edit it."
                )

            updated: list[str] = []
            text_read_back = ""
            if text_mode != "replace":
                try:
                    new_text, text_read_back = partial_text_update(
                        text_mode,
                        getattr(assignment, text_field),
                        field=text_field,
                        content=content,
                        section=section,
                        edits=edits,
                    )
                except SkillEditError as exc:
                    return f"Error: {exc} Nothing was saved."
                setattr(
                    assignment,
                    text_field,
                    new_text.strip() if text_field == "description" else new_text,
                )
                updated.append(f"{text_field} ({text_mode})")
            # Pause provenance: an agent pausing an assignment through
            # this tool used to leave NOTHING behind — no audit row, no state.
            # Capture the pre-update value so the transition can be stamped.
            was_enabled = assignment.enabled
            # A whitespace-only name is "leave unchanged" and the stored name is
            # the stripped form. ``prompt`` is an instruction body, so a
            # whitespace-only value is likewise ignored, but a real value is
            # stored as-is (its internal/trailing formatting is meaningful and
            # must not be trimmed).
            if (name or "").strip():
                assignment.name = (name or "").strip()
                updated.append("name")
            if (prompt or "").strip():
                assignment.prompt = prompt
                updated.append("prompt")
            # Nullable free-text fields: a whitespace-only value means "leave
            # unchanged"; the literal "none" (case-insensitive) clears the
            # field back to the platform/agent default. Prompt bodies
            # (activation gate, ai_label, human_in_the_loop) are stored
            # verbatim - their internal/leading/trailing formatting is
            # meaningful, so they are NOT trimmed. ``description`` is a short
            # label rather than a prompt body, so it IS stored trimmed. The
            # activation gate prompt is a distinct field from the action
            # ``prompt`` - exposing it here is the whole point of this tool
            # (the gate was previously invisible/unsettable through the
            # builder).
            if (activation_prompt or "").strip():
                assignment.activation_prompt = (
                    None
                    if activation_prompt.strip().lower() == "none"
                    else activation_prompt
                )
                updated.append("activation_prompt (gate)")
            if check_provided:
                if check_value is not None:
                    from app.services.fleets import gate_fleet_refusal

                    _gate_refusal = await gate_fleet_refusal(_db, check_value)
                    if _gate_refusal is not None:
                        return f"{_gate_refusal} Nothing was saved."
                assignment.activation_check = check_value
                updated.append("activation_check (tool gate)")
            if (description or "").strip():
                assignment.description = (
                    None
                    if description.strip().lower() == "none"
                    else description.strip()
                )
                updated.append("description")
            if (ai_label_prompt or "").strip():
                assignment.ai_label_prompt = (
                    None
                    if ai_label_prompt.strip().lower() == "none"
                    else ai_label_prompt
                )
                updated.append("ai_label_prompt")
            if (human_in_the_loop_prompt or "").strip():
                assignment.human_in_the_loop_prompt = (
                    None
                    if human_in_the_loop_prompt.strip().lower() == "none"
                    else human_in_the_loop_prompt
                )
                updated.append("human_in_the_loop_prompt")
            if trigger_type and trigger_type not in ASSIGNMENT_TRIGGER_TYPES:
                return (
                    f"Invalid trigger_type: must be one of "
                    f"{', '.join(ASSIGNMENT_TRIGGER_TYPES)}"
                )
            parsed_config: dict | None = None
            if trigger_config:
                parsed_config, config_err = _parse_trigger_config_arg(trigger_config)
                if config_err is not None:
                    return config_err
            if trigger_type or parsed_config is not None:
                # Checked on the trigger as it will be saved: switching to
                # ``schedule`` keeps the stored config, and a new config keeps
                # the stored type. Returning without a commit saves nothing.
                schedule_err = schedule_trigger_error(
                    trigger_type or assignment.trigger_type,
                    assignment.trigger_config if parsed_config is None else parsed_config,
                )
                if schedule_err is not None:
                    return f"{schedule_err} Nothing was saved."
                work_item_err = work_item_trigger_error(
                    trigger_type or assignment.trigger_type,
                    assignment.trigger_config if parsed_config is None else parsed_config,
                )
                if work_item_err is not None:
                    return f"{work_item_err} Nothing was saved."
                platform_event_err = platform_event_trigger_error(
                    trigger_type or assignment.trigger_type,
                    assignment.trigger_config if parsed_config is None else parsed_config,
                )
                if platform_event_err is not None:
                    return f"{platform_event_err} Nothing was saved."
                from app.services.platform_events import fleet_scope_error

                fleet_err = await fleet_scope_error(
                    trigger_type or assignment.trigger_type,
                    assignment.trigger_config if parsed_config is None else parsed_config,
                    assignment.agent_id,
                )
                if fleet_err is not None:
                    return f"{fleet_err} Nothing was saved."
                if parsed_config is not None:
                    from app.services.trigger_provisioning import provision_spec_error

                    provision_err = provision_spec_error(trigger_type or assignment.trigger_type, parsed_config)
                    if provision_err is not None:
                        return f"{provision_err} Nothing was saved."
            if parsed_config is not None:
                # Only an integration the new trigger ADDS is checked, like the
                # REST update: the stored one is never re-judged.
                trigger_err = await _trigger_integration_error(
                    _db, uid, parsed_config, baseline=assignment.trigger_config
                )
                if trigger_err is not None:
                    return f"{trigger_err} Nothing was saved."
            if trigger_type:
                assignment.trigger_type = trigger_type
                updated.append("trigger_type")
                # Switching TO the work_item trigger switches the agent's
                # ``work_items`` built-in on, like the REST update:
                # on the transition only, so a deliberate uncheck sticks.
                if trigger_type == "work_item" and _agent is not None:
                    _bt_ids = list(getattr(_agent, "builtin_tool_ids", None) or [])
                    if "work_items" not in _bt_ids:
                        _agent.builtin_tool_ids = [*_bt_ids, "work_items"]
            if parsed_config is not None:
                assignment.trigger_config = parsed_config
                updated.append("trigger_config")
            # Mirror the REST API's ``update_assignment``: a trigger edit that
            # leaves a webhook assignment without a secret gets one generated
            # server-side (SEC-5). Scoped to trigger edits only so unrelated
            # updates never silently flip a legacy secretless assignment. The
            # matching mode is the model's (``settle_event_matching`` on save):
            # the config is stored as sent, so an edit that leaves the events
            # alone keeps a trigger saved before exact matching as it was.
            if "trigger_type" in updated or "trigger_config" in updated:
                from app.core.utils import ensure_webhook_secret

                assignment.trigger_config = ensure_webhook_secret(
                    assignment.trigger_type, assignment.trigger_config
                )
            if (delay or "").strip():
                # Strip for both the "provided" check and the clear sentinel so a
                # padded "  none  " clears the delay and whitespace isn't stored.
                _delay = delay.strip()
                assignment.delay = None if _delay.lower() == "none" else _delay
                updated.append("delay")

            # Human-in-the-loop is tri-state: 'true' / 'false' / 'inherit'
            # (the last clears the override back to NULL so it inherits from the
            # parent agent). This is the on/off boolean - distinct from the
            # ``human_in_the_loop_prompt`` override handled above.
            hitl_value, hitl_err, hitl_provided = _parse_optional_bool_arg(
                human_in_the_loop, allow_inherit=True
            )
            if hitl_provided:
                if hitl_err:
                    return f"Invalid human_in_the_loop: {hitl_err}"
                assignment.human_in_the_loop = hitl_value
                updated.append("human_in_the_loop")

            # Plain on/off behavioural toggles - same surface as the REST API.
            # ``enabled`` is parsed here too (not via a bare ``.lower()``) so a
            # native JSON bool from a verbatim-forwarded tool call is accepted
            # rather than crashing with AttributeError.
            from app.services.assignment_pause import is_emergency_stopped

            for _bool_field, _bool_raw in (
                ("enabled", enabled),
                ("ai_label", ai_label),
                ("notify_on_complete", notify_on_complete),
                ("show_in_digest", show_in_digest),
                ("dry_run", dry_run),
                ("ignore_self_triggered_events", ignore_self_triggered_events),
                ("retry_on_failure", retry_on_failure),
            ):
                _bool_val, _bool_err, _bool_provided = _parse_optional_bool_arg(
                    _bool_raw
                )
                if not _bool_provided:
                    continue
                if _bool_err:
                    return f"Invalid {_bool_field}: {_bool_err}"
                if (
                    _bool_field == "enabled"
                    and _bool_val is True
                    and is_emergency_stopped(assignment)
                ):
                    # Same boundary as the REST route: an assignment the
                    # emergency stop paused is only resumed by lifting the
                    # stop, with a recorded reason — never one at a time by
                    # an agent.
                    return (
                        "This assignment was paused by an emergency stop and "
                        "cannot be re-enabled here. An administrator (or the "
                        "user, for their own scope) must lift the emergency "
                        "stop from the profile menu or the banner."
                    )
                setattr(assignment, _bool_field, _bool_val)
                updated.append(_bool_field)

            # Safety / limit integers - range-guarded so the LLM can't persist a
            # runaway iteration cap or a negative timeout.
            for _int_field, _int_raw, _int_min, _int_max in (
                ("timeout_seconds", timeout_seconds, 0, 3600),
                ("max_retries", max_retries, 0, 10),
            ):
                _int_val, _int_err, _int_provided = _parse_optional_int_arg(
                    _int_raw, _int_field, minimum=_int_min, maximum=_int_max
                )
                if not _int_provided:
                    continue
                if _int_err:
                    return _int_err
                setattr(assignment, _int_field, _int_val)
                updated.append(_int_field)

            # max_iterations is tri-state for assignments: a number sets an
            # explicit per-run override (OVERRIDES the agent), "" leaves it
            # unchanged, and "none"/"inherit"/"default" clears it back to NULL so
            # it inherits (agent override -> platform default). A real value is
            # clamped to the shared ceiling.
            from app.models.general_assistant_config import (
                MAX_ITERATIONS_CEILING,
                clamp_max_iterations,
            )

            if isinstance(max_iterations, str) and max_iterations.strip().lower() in (
                "none",
                "inherit",
                "default",
            ):
                assignment.max_iterations = None
                updated.append("max_iterations")
            else:
                _mi_val, _mi_err, _mi_provided = _parse_optional_int_arg(
                    max_iterations,
                    "max_iterations",
                    minimum=1,
                    maximum=MAX_ITERATIONS_CEILING,
                )
                if _mi_provided:
                    if _mi_err:
                        return _mi_err
                    assignment.max_iterations = (
                        clamp_max_iterations(_mi_val)
                        if _mi_val is not None
                        else None
                    )
                    updated.append("max_iterations")

            # wait_policy: wins over the agent's; "none" clears it.
            _wp_val, _wp_err, _wp_provided = _parse_wait_policy_arg(wait_policy)
            if _wp_provided:
                if _wp_err:
                    return _wp_err
                assignment.wait_policy = _wp_val
                updated.append("wait_policy")

            # organization_context_mode — inherit/always/off; wins over the agent's (#11533).
            if isinstance(organization_context_mode, str) and organization_context_mode.strip():
                from app.services.organization_context_injection import check_mode

                try:
                    assignment.organization_context_mode = check_mode(organization_context_mode)
                except ValueError as exc:
                    return f"Invalid {exc}."
                updated.append("organization_context_mode")
            # organization_context_categories: a list, "none" (empty) or "inherit" (#11533).
            if organization_context_categories is not None and organization_context_categories != "":
                from app.services.organization_context_categories import check_categories

                try:
                    assignment.organization_context_categories = check_categories(organization_context_categories)
                except ValueError as exc:
                    return f"Invalid {exc}."
                updated.append("organization_context_categories")

            if not updated:
                return "No fields to update. Provide at least one field."

            # Pause provenance. Stamp WHO/WHEN/WHY on a pause performed
            # by an agent, and audit it as the user the agent is acting as —
            # otherwise "who paused this?" has no answer at all for this path.
            # Best-effort: a failure to resolve the acting user must not block
            # the update the LLM asked for.
            if "enabled" in updated and was_enabled != assignment.enabled:
                from sqlalchemy import select as _select

                from app.models.user import User as _User
                from app.services.assignment_pause import (
                    actor_display_name as _actor_display_name,
                )
                from app.services.assignment_pause import (
                    pause_assignment as _pause_assignment,
                )
                from app.services.assignment_pause import (
                    record_pause_audit as _record_pause_audit,
                )
                from app.services.assignment_pause import (
                    resume_assignment as _resume_assignment,
                )
                from app.services.assignment_pause import (
                    switch_off_reason as _switch_off_reason,
                )

                _actor_res = await _db.execute(
                    _select(_User).where(_User.id == uid)
                )
                acting_user = _actor_res.scalar_one_or_none()
                # Same label rule as every other caller: a display name, not
                # an email. This banner renders to everyone who can read the
                # assignment, so an agent-driven pause must not leak the acting
                # user's email where a UI pause shows only their name.
                actor_label = (
                    _actor_display_name(acting_user) if acting_user else str(uid)
                )
                if not assignment.enabled:
                    _pause_details = (
                        "Paused by an agent through the agent-builder "
                        f"'update_assignment' tool, acting as {actor_label}."
                    )
                    # Off before its first run: still being set up, so its
                    # fleet reads "Not set up", not "Paused" (#11060).
                    _toggle_reason = _switch_off_reason(assignment, "agent_tool")
                    _pause_assignment(
                        assignment,
                        reason=_toggle_reason,
                        details=_pause_details,
                        actor_user_id=uid,
                        actor_label=actor_label,
                    )
                    if acting_user is not None:
                        await _record_pause_audit(
                            _db,
                            assignment=assignment,
                            user=acting_user,
                            reason=_toggle_reason,
                            details=_pause_details,
                        )
                elif acting_user is not None:
                    await _resume_assignment(
                        _db, assignment=assignment, user=acting_user
                    )
                else:
                    # Unresolvable actor (deleted user): still drop the stamp,
                    # or an *enabled* assignment would keep claiming it is
                    # paused.
                    from app.services.assignment_pause import (
                        clear_pause_provenance as _clear_pause_provenance,
                    )
                    from app.services.assignment_pause import (
                        mark_schedule_resumed as _mark_schedule_resumed,
                    )

                    _clear_pause_provenance(assignment)
                    _mark_schedule_resumed(assignment)

            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "update_assignment commit failed for assignment %s by user %s",
                    assignment_id,
                    user_id,
                )
                await _db.rollback()
                return f"Failed to update assignment `{assignment_id}`. Please try again."
            if "trigger_config" in updated:
                # A filter rewritten by hand is a friction of the run: the
                # generic fix belongs in the trigger, not in one edit.
                from app.services.frictions import note_trigger_config_edit

                note_trigger_config_edit(
                    "update_assignment", assignment.name, parsed_config, fix_applied=str(fix_applied or "")
                )
            _assignment_result = f"Assignment **{assignment.name}** updated: {', '.join(updated)}."
            if text_read_back:
                _assignment_result += f"\n{text_read_back}"
            return _assignment_result

    # ── delete_assignment ─────────────────────────────────────────────

    async def _delete_assignment(assignment_id: str, after_run: object = None) -> str:
        from app.core.database import async_session as _builder_session

        defer, defer_err, _ = _parse_bool_arg(after_run, "after_run")
        if defer_err:
            return defer_err

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Assignment deletion is not available for anonymous users."

        assignment_uuid, id_err = _validate_id_arg(assignment_id, "assignment_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            # Same gate as the REST API's ``delete_assignment``.
            assignment, _agent, perm_err = await _load_writable_assignment_for_builder(
                _db, uid, assignment_uuid, action="delete"
            )
            if perm_err is not None or assignment is None:
                return perm_err or (
                    f"Assignment `{assignment_id}` not found or you don't have "
                    "permission to delete it."
                )

            wf_name = assignment.name
            # A run in flight would be deleted with the row (FK cascade) and
            # end "no longer recorded": refuse and name it.
            blocking_runs = await _active_runs_blocking_delete(_db, assignment.id)
            if blocking_runs and defer:
                return await _schedule_assignment_retirement(
                    _db, uid, assignment, assignment_id, blocking_runs
                )
            if blocking_runs:
                await _db.rollback()
                return _delete_blocked_by_runs_message(
                    assignment_id, wf_name, blocking_runs
                )
            # A deleted seeded fleet health check stays deleted: its fleet opts out.
            from app.services.fleets import record_health_check_deleted

            await record_health_check_deleted(_db, assignment, actor=str(uid))
            await _db.delete(assignment)
            try:
                await _db.commit()
            except Exception:
                # Same pattern as ``_delete_agent`` / ``_update_assignment``:
                # log full detail server-side, return a generic retry
                # message to the tool caller, and rollback the dedicated
                # session before the context manager closes it.
                logger.exception(
                    "delete_assignment commit failed for assignment %s by user %s",
                    assignment_id,
                    user_id,
                )
                await _db.rollback()
                return f"Failed to delete assignment `{assignment_id}`. Please try again."
            return f"Assignment **{wf_name}** has been deleted."

    # ── trigger_assignment ────────────────────────────────────────────

    async def _trigger_assignment_tool(assignment_id: str, payload: str = "") -> str:
        import json as json_mod

        from sqlalchemy import select as sa_select

        from app.api.v1.helpers import (
            active_run_refusal,
            find_user_assignment,
            get_user_group_ids,
            manual_trigger_overlap,
            manual_trigger_refusal,
            manual_trigger_run_scope,
            manual_trigger_skipped,
        )
        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Triggering assignments is not available for anonymous users."
        owed_conversation_id = safe_uuid(conversation_id) if conversation_id else None

        assignment_uuid, id_err = await _id_arg(assignment_id, "assignment_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid)
            caller = await _load_caller_user_for_builder(_db, uid)
            is_admin = caller is not None and caller.is_admin

            # Existence and visibility in ONE lookup: an assignment the caller
            # cannot see must answer exactly like an unknown id. An
            # admin reaches every assignment, as on the REST trigger route
            # (unless AGENT_TOOLS_STRICT_ADMIN_ACCESS).
            assignment = await find_user_assignment(
                _db, uid, assignment_uuid, group_ids, is_admin=agent_tool_admin_bypass(caller)
            )
            agent = None
            if assignment is not None:
                agent_result = await _db.execute(
                    sa_select(Agent).where(Agent.id == assignment.agent_id)
                )
                agent = agent_result.scalar_one_or_none()
            if assignment is None or agent is None:
                return (
                    f"Assignment `{assignment_id}` not found or you don't " "have access."
                )

            # The REST ``trigger_assignment_manual`` gate, shared: enabled
            # assignment and agent, company-managed admin-only unless it runs
            # per user, then the active-run guard (the caller's own run for a
            # per-user assignment).
            refusal = manual_trigger_refusal(assignment, agent, is_admin=is_admin)
            if refusal is not None:
                return _trigger_refusal_message(refusal, assignment, agent)

            from app.services.assignment_pause import (
                OWNER_DEACTIVATED_MESSAGE,
                AssignmentOwnerDeactivatedError,
                is_paused_by_deactivated_owner,
            )

            paused_answer = (
                f"Assignment **{assignment.name}** was not triggered. {OWNER_DEACTIVATED_MESSAGE}"
            )
            # Paused while its owner is deactivated, like the REST
            # trigger route: after the shared gate, before the overlap guard.
            if await is_paused_by_deactivated_owner(_db, assignment):
                return paused_answer

            refusal = await manual_trigger_overlap(_db, assignment, agent, uid)
            if refusal is not None:
                return _trigger_refusal_message(refusal, assignment, agent)

            trigger_payload: dict = {}
            if payload:
                try:
                    trigger_payload = json_mod.loads(payload)
                except json_mod.JSONDecodeError:
                    return "Invalid payload: must be valid JSON."

            try:
                from app.services.assignment_engine import (
                    trigger_assignment as _do_trigger,
                )

                run_scope = manual_trigger_run_scope(assignment, agent, uid)
                run_id = await _do_trigger(
                    assignment=assignment,
                    agent=agent,
                    payload=trigger_payload,
                    db=_db,
                    trigger_context={"source": "agent_builder"},
                    # Chat-initiated trigger runs on behalf of the caller, not
                    # the assignment's "run as" user.
                    triggered_by_user_id=uid,
                    # The engine's lock makes the overlap check above atomic.
                    skip_if_active=True,
                    active_run_user_id=run_scope,
                    # Told here how the run ended.
                    owed_conversation_id=owed_conversation_id,
                )
                if run_id is None:
                    refusal = await active_run_refusal(
                        _db, assignment.id, run_scope
                    ) or manual_trigger_skipped(run_scope)
                    return _trigger_refusal_message(refusal, assignment, agent)
                told = (
                    "\n  • This conversation is told when the run ends."
                    if owed_conversation_id is not None
                    else ""
                )
                return (
                    f"Assignment **{assignment.name}** triggered successfully!\n"
                    f"  • Run ID: `{run_id}`\n"
                    f"  • Agent: {agent.name}{told}"
                )
            except AssignmentOwnerDeactivatedError:
                # The owner was deactivated since the check above.
                return paused_answer
            except Exception as e:
                # Log the underlying exception server-side (with stack
                # trace) for triage but DON'T surface the raw repr to
                # the LLM/end-user.  Internal exception strings can
                # leak:
                #
                # * DB constraint names hinting at schema layout.
                # * Connection strings / hosts in pool-timeout
                #   diagnostics.
                # * Provider-specific error payloads that may contain
                #   sensitive identifiers from upstream services.
                #
                # All sibling agent-builder handlers (``_update_agent``,
                # ``_update_assignment``, ``_delete_agent``,
                # ``_delete_assignment``) return a generic retry
                # message — keeping ``_trigger_assignment_tool``
                # consistent prevents callers from special-casing the
                # error parsing logic by handler.
                logger.error(
                    "Agent builder trigger_assignment failed: %s",
                    format_exception_message(e),
                    exc_info=True,
                )
                # ``assignment_engine.trigger_assignment`` performs
                # writes/commits using the passed session and does NOT
                # roll back on its own failure path.  Swallowing the
                # exception without a rollback would leave ``_db`` in a
                # failed-transaction state, and when the
                # ``async with _builder_session()`` closes the session
                # the pooled connection would be returned in a poisoned
                # state.  Mirror the explicit rollback used by the
                # sibling ``_update_*`` / ``_delete_*`` handlers so the
                # connection always returns clean to the pool.
                await _db.rollback()
                return (
                    f"Failed to trigger assignment **{assignment.name}**. "
                    "Please retry; if the issue persists, contact support."
                )

    # ── cancel_runs ───────────────────────────────────────────────────

    async def _cancel_runs(
        agent_id: str = "",
        assignment_id: str = "",
        status: str = "",
        created_before: str = "",
        dry_run: bool = False,
    ) -> str:
        """Admin: cancel the waiting runs of an agent / assignment."""
        from datetime import datetime

        from app.api.v1.helpers import agent_tool_admin_bypass
        from app.core.database import async_session as _builder_session
        from app.services.audit import log_audit
        from app.services.run_bulk_cancel import (
            CANCELLABLE_RUN_STATUSES,
            BulkCancelFilter,
            BulkCancelFilterError,
            cancel_waiting_runs,
            count_cancellable_runs,
        )

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Cancelling runs is not available for anonymous users."

        agent_uuid = assignment_uuid = None
        if agent_id:
            agent_uuid, id_err = _validate_id_arg(agent_id, "agent_id")
            if id_err:
                return id_err
        if assignment_id:
            assignment_uuid, id_err = _validate_id_arg(assignment_id, "assignment_id")
            if id_err:
                return id_err
        statuses = tuple(
            dict.fromkeys(s.strip().lower() for s in (status or "").split(",") if s.strip())
        ) or CANCELLABLE_RUN_STATUSES
        before = None
        if created_before:
            try:
                before = datetime.fromisoformat(created_before.strip())
            except ValueError:
                return (
                    "created_before must be an ISO-8601 date-time (e.g. "
                    f"'2026-11-03T08:00:00Z'). Got: {_bounded_display(created_before)}"
                )
        try:
            filters = BulkCancelFilter(
                agent_id=agent_uuid,
                assignment_id=assignment_uuid,
                statuses=statuses,
                created_before=before,
            )
        except BulkCancelFilterError as exc:
            return str(exc)

        async with _builder_session() as _db:
            caller = await _load_caller_user_for_builder(_db, uid)
            if not agent_tool_admin_bypass(caller):
                return (
                    "Only admins can cancel runs in bulk (and an admin's agent "
                    "cannot while AGENT_TOOLS_STRICT_ADMIN_ACCESS is set)."
                )
            if dry_run:
                count = await count_cancellable_runs(_db, filters)
                return (
                    f"{count} run(s) in status {', '.join(filters.statuses)} match; "
                    "nothing was cancelled (dry_run). Call again without dry_run "
                    "to cancel them."
                )
            actor = caller.full_name or caller.email
            try:
                count = await cancel_waiting_runs(_db, filters, actor=actor)
                await log_audit(
                    _db,
                    user=caller,
                    action="bulk_cancel_runs",
                    entity_type="assignment_run",
                    summary=f"Cancelled {count} waiting run(s) via the cancel_runs tool",
                    details={
                        "cancelled_count": count,
                        "agent_id": agent_id or None,
                        "assignment_id": assignment_id or None,
                        "status": list(filters.statuses),
                        "created_before": before.isoformat() if before else None,
                    },
                )
                await _db.commit()
            except Exception as e:
                logger.error(
                    "Agent builder cancel_runs failed: %s",
                    format_exception_message(e),
                    exc_info=True,
                )
                await _db.rollback()
                return "Failed to cancel the runs; nothing was changed. Please retry."
        return (
            f"Cancelled {count} run(s) in status {', '.join(filters.statuses)} "
            f"(reason: 'Cancelled by {actor}'). Runs already running were left alone."
        )

    # ── run history inside an A2A partner task ────────────────────────
    #
    # The builder acts as an anonymous caller there (``partner_scope``); the
    # one read lent back is the partner's own tasks' runs (``a2a.partner_runs``),
    # scoped by the partner principal, never by the run-as identity. ``None``:
    # not in a partner task with a known partner, the normal path runs.

    async def _partner_list_runs(
        agent_id: str, assignment_id: str, status: str, search: str, scope: str, limit: int, offset: int
    ) -> str | None:
        from app.a2a.partner_runs import list_partner_runs
        from app.a2a.partner_scope import current_partner

        partner = current_partner()
        if partner is None:
            return None
        if assignment_id or (search or "").strip() or (scope or "visible").strip().lower() != "visible":
            return (
                "In a partner task, list_runs lists the runs of your own tasks only: "
                "filter by agent_id or status, without assignment_id, search or scope."
            )
        agent_uuid = None
        if agent_id:
            agent_uuid, id_err = _validate_id_arg(agent_id, "agent_id")
            if id_err:
                return id_err
        limit, offset, page_err = _coerce_page_args(limit, offset, default_limit=20)
        if page_err:
            return page_err
        status = (status or "").strip().lower()
        if status and status not in _run_status_vocabulary():
            return f"Invalid status `{status}`. Use one of: {', '.join(_run_status_vocabulary())}."
        return await list_partner_runs(partner, agent_id=agent_uuid, status=status, limit=limit, offset=offset)

    async def _partner_get_run(run_id: str) -> str | None:
        from app.a2a.partner_runs import get_partner_run
        from app.a2a.partner_scope import current_partner

        partner = current_partner()
        if partner is None:
            return None
        # A full id only: a prefix would resolve against the run-as reach.
        run_uuid, id_err = _validate_id_arg(run_id, "run_id")
        if run_uuid is None:
            return id_err
        return await get_partner_run(partner, run_uuid)

    # ── list_runs ─────────────────────────────────────────────────────

    async def _list_runs(
        agent_id: str = "",
        assignment_id: str = "",
        status: str = "",
        search: str = "",
        scope: str = "visible",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        from sqlalchemy import Text, cast, func, or_, select

        from app.api.v1.helpers import run_visible_filter
        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.assignment import Assignment, AssignmentRun

        partner_reply = await _partner_list_runs(agent_id, assignment_id, status, search, scope, limit, offset)
        if partner_reply is not None:
            return partner_reply

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Run history is not available for anonymous users."

        agent_uuid = None
        if agent_id:
            agent_uuid, id_err = await _id_arg(agent_id, "agent_id")
            if id_err:
                return id_err
        assignment_uuid = None
        if assignment_id:
            assignment_uuid, id_err = await _id_arg(
                assignment_id, "assignment_id"
            )
            if id_err:
                return id_err

        limit, offset, page_err = _coerce_page_args(
            limit, offset, default_limit=20
        )
        if page_err:
            return page_err

        _known_statuses = _run_status_vocabulary()
        status = (status or "").strip().lower()
        if status and status not in _known_statuses:
            return (
                f"Invalid status `{status}`. Use one of: "
                f"{', '.join(_known_statuses)}."
            )

        scope = (scope or "visible").strip().lower()
        if scope not in ("visible", "all"):
            return (
                f"Invalid scope `{scope}`. Use 'visible' (default, your own "
                "runs + company-managed runs) or 'all' (admin only, every "
                "run on the platform)."
            )

        # Normalize the search term: a whitespace-only value must not enter
        # search mode (it would ILIKE-match every row — an expensive scan
        # returning misleading "matches") — treat it as no search at all.
        search = (search or "").strip()

        async with _builder_session() as _db:
            admin_all = False
            if scope == "all":
                caller = await _load_caller_user_for_builder(_db, uid)
                if not agent_tool_admin_bypass(caller):
                    return (
                        "Only admins can list ALL runs across the platform (and "
                        "an admin's agent cannot while "
                        "AGENT_TOOLS_STRICT_ADMIN_ACCESS is set). Call "
                        "list_runs without scope (or scope='visible') to see "
                        "your own runs and company-managed runs."
                    )
                admin_all = True

            # Run visibility is intentionally NARROWER than agent visibility:
            # own + company-managed only (``run_visible_filter`` — the same
            # single source of truth the REST runs list uses). ``scope='all'``
            # mirrors the REST run-detail contract, where an admin may open
            # any run by id — this is a listing convenience over an access
            # the admin already has, not a new permission.
            q = select(AssignmentRun)
            if not admin_all:
                q = q.where(run_visible_filter(uid))
            if agent_uuid is not None:
                q = q.where(AssignmentRun.agent_id == agent_uuid)
            if assignment_uuid is not None:
                q = q.where(AssignmentRun.assignment_id == assignment_uuid)
            if status:
                q = q.where(AssignmentRun.status == status)
            if search:
                # The outer joins exist only to serve the cross-entity
                # search (names for display are batch-resolved separately
                # below), so the common "recent runs" path — including its
                # count-from-subquery — skips them entirely. Both joins are
                # many-to-one from the run side, so they can never multiply
                # result rows.
                #
                # Beyond names + result summary, the search deliberately
                # covers the run's *content*: ``trigger_payload`` (webhook /
                # event bodies — the only way to answer "was a run ever
                # triggered for X?"), ``actions_taken`` and ``error``.
                # The huge ``messages`` conversation log is
                # intentionally NOT part of the cross-run search — use
                # ``get_run`` with ``search`` for per-run deep inspection.
                # ``_webhook_meta`` (internal routing metadata) is subtracted
                # from the trigger-payload predicate so cross-run matching
                # aligns with the in-run search's "safe to surface" contract
                # (``_iter_run_search_sections`` strips the same key) and
                # internal metadata can neither match nor be probed — see
                # ``_searchable_trigger_payload``.
                pattern = f"%{search}%"
                payload_searchable = _searchable_trigger_payload()
                q = (
                    q.join(
                        Agent, AssignmentRun.agent_id == Agent.id, isouter=True
                    )
                    .join(
                        Assignment,
                        AssignmentRun.assignment_id == Assignment.id,
                        isouter=True,
                    )
                    .where(
                        or_(
                            Agent.name.ilike(pattern),
                            Assignment.name.ilike(pattern),
                            AssignmentRun.result_summary.ilike(pattern),
                            AssignmentRun.error.ilike(pattern),
                            payload_searchable.ilike(pattern),
                            cast(AssignmentRun.actions_taken, Text).ilike(
                                pattern
                            ),
                        )
                    )
                )

            total: int | None = None
            has_more = False
            if search:
                # The deep-search predicates (ILIKE over the ``error`` text
                # and the JSON casts) cannot use an index, so an exact
                # ``COUNT(*)`` would force a SECOND full scan per call.
                # Fetch ``limit + 1`` rows instead — the ordered page query
                # can stop early on the ``created_at`` index — and report
                # "more results exist" without an exact total.
                try:
                    result = await _db.execute(
                        q.order_by(AssignmentRun.created_at.desc(), AssignmentRun.id).limit(limit + 1).offset(offset)
                    )
                except Exception as exc:
                    # A rare term over a large run table makes that scan
                    # outlast the database statement timeout: answer how to
                    # narrow it instead of an opaque error the model retries.
                    if not is_statement_timeout(exc):
                        raise
                    unset = [
                        name
                        for name, value in (
                            ("agent_id", agent_uuid),
                            ("assignment_id", assignment_uuid),
                            ("status", status),
                        )
                        if not value
                    ]
                    narrow = f"add {', '.join(unset)}" if unset else "use a more specific term"
                    return (
                        f"The search for `{search}` timed out in the "
                        "database: the searched fields have no index, so a "
                        "rare term scans every run. Do not retry the same "
                        f"call. Narrow it ({narrow}), or use get_run with "
                        "`search` on run ids you already have."
                    )
                runs = result.scalars().all()
                has_more = len(runs) > limit
                runs = runs[:limit]
                if not runs:
                    if offset:
                        return (
                            f"No more runs matching `{search}` at offset "
                            f"{offset}. Use a smaller offset."
                        )
                    # A bare "No runs found." sends the model into a loop of
                    # search-term variants — spell out what was
                    # searched and what to try instead.
                    return (
                        f"No runs matched search `{search}` (searched agent "
                        "name, assignment name, result summary, error, "
                        "trigger payload, and actions taken — NOT the "
                        "conversation log). Next steps: (1) if you already "
                        "have candidate run ids, use get_run with the "
                        "`search` argument to grep inside a single run "
                        "including its conversation log; (2) list runs by "
                        "agent_id/assignment_id and a time window instead of "
                        "retrying search variants; (3) if a run for an "
                        "expected event (e.g. a webhook) does not appear "
                        "here, the most likely explanation is that the event "
                        "was never received or the assignment did not "
                        "trigger — report that finding instead of searching "
                        "again."
                    )
            else:
                total = (
                    await _db.execute(
                        select(func.count()).select_from(q.subquery())
                    )
                ).scalar() or 0
                if not total:
                    return "No runs found."

                result = await _db.execute(
                    q.order_by(
                        AssignmentRun.created_at.desc(), AssignmentRun.id
                    )
                    .limit(limit)
                    .offset(offset)
                )
                runs = result.scalars().all()
                if not runs:
                    return (
                        f"No runs on this page (offset {offset} of {total} "
                        "total). Use a smaller offset."
                    )

            # Batch-resolve agent / assignment names for display.
            run_agent_ids = {r.agent_id for r in runs if r.agent_id}
            run_wf_ids = {r.assignment_id for r in runs if r.assignment_id}
            agent_names: dict = {}
            if run_agent_ids:
                rows = await _db.execute(
                    select(Agent.id, Agent.name).where(
                        Agent.id.in_(run_agent_ids)
                    )
                )
                agent_names = {row[0]: row[1] for row in rows.fetchall()}
            wf_names: dict = {}
            if run_wf_ids:
                rows = await _db.execute(
                    select(Assignment.id, Assignment.name).where(
                        Assignment.id.in_(run_wf_ids)
                    )
                )
                wf_names = {row[0]: row[1] for row in rows.fetchall()}

            scope_label = "all runs (admin)" if admin_all else "visible runs"
            first = offset + 1
            last = offset + len(runs)
            if total is not None:
                header = (
                    f"Found {total} run(s) [{scope_label}] — "
                    f"showing {first}-{last}, most recent first:\n"
                )
            else:
                # Search mode has no exact total (see the COUNT(*) note
                # above) — "N+" signals that more matches exist.
                plus = "+" if has_more else ""
                header = (
                    f"Found {len(runs)}{plus} run(s) matching `{search}` "
                    f"[{scope_label}] — showing {first}-{last}, most recent "
                    "first:\n"
                )
            parts: list[str] = [header]
            for r in runs:
                agent_name = agent_names.get(r.agent_id, "Unknown agent")
                wf_name = (
                    wf_names.get(r.assignment_id, "Unknown assignment")
                    if r.assignment_id
                    else "(no assignment)"
                )
                created = r.created_at.isoformat() if r.created_at else "?"
                line = (
                    f"• `{r.id}` — **{agent_name}** / {wf_name}\n"
                    f"  Status: {r.status} | Created: {created}"
                )
                if r.completed_at and r.started_at:
                    secs = (r.completed_at - r.started_at).total_seconds()
                    line += f" | Duration: {secs:.0f}s"
                line += _run_degraded_lines(r, prefix="\n  ")
                if r.status == "failed" and r.error:
                    err_text = " ".join(r.error.split())
                    if len(err_text) > 200:
                        err_text = err_text[:200] + "…"
                    line += f"\n  Error: {err_text}"
                elif r.result_summary:
                    summary = " ".join(r.result_summary.split())
                    if len(summary) > 200:
                        summary = summary[:200] + "…"
                    line += f"\n  Summary: {summary}"
                parts.append(line)
            if total is not None and last < total:
                parts.append(
                    f"\n{total - last} more run(s). Call list_runs again "
                    f"with offset={last} for the next page."
                )
            elif has_more:
                parts.append(
                    f"\nMore matching run(s) exist. Call list_runs again "
                    f"with offset={last} for the next page."
                )
            parts.append(
                "\nUse get_run with a run id for full details "
                "(complete result, error, token usage)."
            )
            return "\n".join(parts)

    # ── get_run ───────────────────────────────────────────────────────

    async def _get_run(run_id: str, search: str = "") -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import openable_parent_runs, run_visible_filter
        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.assignment import Assignment, AssignmentRun
        from app.services.assignment_engine import TERMINAL_RUN_STATUSES
        from app.services.assignment_run_wake import mark_told

        partner_reply = await _partner_get_run(run_id)
        if partner_reply is not None:
            return partner_reply

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Run history is not available for anonymous users."

        run_uuid, id_err = await _id_arg(run_id, "run_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            # Mirror the REST ``GET /assignments/runs/{run_id}`` contract:
            # admins can open any run; everyone else sees their own runs and
            # company-managed runs. Existence-hiding by design — invisible
            # and non-existent runs return the same message. The caller row
            # is loaded lazily (only on a visibility miss) so the common
            # visible-run path costs no extra query — same pattern as
            # ``get_agent``'s admin fallback.
            result = await _db.execute(
                select(AssignmentRun).where(
                    AssignmentRun.id == run_uuid, run_visible_filter(uid)
                )
            )
            run = result.scalar_one_or_none()
            if run is None:
                caller = await _load_caller_user_for_builder(_db, uid)
                if agent_tool_admin_bypass(caller):
                    result = await _db.execute(
                        select(AssignmentRun).where(AssignmentRun.id == run_uuid)
                    )
                    run = result.scalar_one_or_none()
            if run is None:
                return f"Run `{run_uuid}` not found."

            # The parent is named only to a caller who could open it, under
            # the same rule as the run itself.
            parent_shown = True
            if run.parent_run_id:
                openable = await openable_parent_runs(
                    _db, {run.parent_run_id}, uid, is_admin=False
                )
                if run.parent_run_id not in openable:
                    caller = await _load_caller_user_for_builder(_db, uid)
                    parent_shown = agent_tool_admin_bypass(caller)

            # Deep in-run search: grep the run's FULL content —
            # including the conversation log and trigger payload that the
            # detail view below deliberately does not dump — and return
            # bounded excerpts instead of the whole record.
            if search and search.strip():
                return _search_run_content(
                    run, search.strip(), hide_parent=not parent_shown
                )

            agent_name = None
            if run.agent_id:
                agent_name = (
                    await _db.execute(
                        select(Agent.name).where(Agent.id == run.agent_id)
                    )
                ).scalar_one_or_none()
            wf_name = None
            if run.assignment_id:
                wf_name = (
                    await _db.execute(
                        select(Assignment.name).where(
                            Assignment.id == run.assignment_id
                        )
                    )
                ).scalar_one_or_none()

            trigger_source = None
            if isinstance(run.trigger_context, dict):
                trigger_source = run.trigger_context.get(
                    "type"
                ) or run.trigger_context.get("source")

            parts = [
                f"# Run `{run.id}`",
                "",
                f"Agent: **{agent_name or 'Unknown'}** (id: `{run.agent_id}`)",
                (
                    f"Assignment: **{wf_name}** (id: `{run.assignment_id}`)"
                    if run.assignment_id
                    else "Assignment: (none — direct/delegated run)"
                ),
                f"Status: {run.status}",
                *(
                    # A completed run that ended on a limit: its
                    # result is partial, not the finished task.
                    [f"Stopped before finishing: {run.stop_reason} (partial result)"]
                    if run.stop_reason and run.stop_reason != UNREADABLE_STOP_REASON
                    else []
                ),
                # Integration auth failures: a degraded run, never a plain
                # success (#1589).
                *[ln for ln in _run_degraded_lines(run, prefix="\n").split("\n") if ln],
                f"Trigger: {trigger_source or 'unknown'}",
                f"Company-managed: {'Yes' if run.is_company_managed else 'No'}",
            ]
            if run.parent_run_id and parent_shown:
                parts.append(f"Parent run: `{run.parent_run_id}` (delegation)")
            parts.append("")
            parts.append(f"Created: {run.created_at}")
            if run.started_at:
                parts.append(f"Started: {run.started_at}")
            event_line = _event_latency_line(run)
            if event_line:
                parts.append(event_line)
            if run.completed_at:
                parts.append(f"Completed: {run.completed_at}")
                if run.started_at:
                    secs = (run.completed_at - run.started_at).total_seconds()
                    parts.append(f"Duration: {secs:.0f}s")
            from app.services.hitl_resume_clock import awaiting_human_seconds

            _paused = awaiting_human_seconds(run)
            if _paused:
                # Counted apart: a pause for a person is neither queue nor work time (#1645).
                parts.append(f"Waiting for a person: {_paused:.0f}s (within the duration)")
            if run.total_input_tokens or run.total_output_tokens:
                parts.append(
                    f"Tokens: {run.total_input_tokens or 0} in / "
                    f"{run.total_output_tokens or 0} out"
                )
            if run.retry_count:
                parts.append(f"Retries: {run.retry_count}")
            if run.actions_taken:
                parts.append(f"Actions taken: {len(run.actions_taken)}")
            if run.human_input_question:
                parts.append("")
                parts.append("Human input requested:")
                parts.append(run.human_input_question)
                if run.human_input_response:
                    parts.append("Human response:")
                    parts.append(run.human_input_response)
            parts.extend(_human_input_history_lines(run))
            if run.error:
                parts.append("")
                parts.append("Error:")
                parts.append(run.error)
            # Result in FULL (never truncated here) so the caller can quote
            # "in your last run you said…" faithfully — mirrors get_agent's
            # full-persona contract. The agent loop's per-result cap
            # (_truncate_tool_result) remains the context-safety net.
            # ``messages`` / ``trigger_payload`` are deliberately NOT dumped:
            # they can be huge and may embed sensitive upstream payloads.
            parts.append("")
            parts.append("Result (full):")
            parts.append(run.result_summary or "None")
            run_ended = run.status in TERMINAL_RUN_STATUSES
        if run_ended and conversation_id:
            # The agent now knows how the run ended: the wake has nothing
            # left to tell this conversation.
            await mark_told([str(run.id)], conversation_id)
        return "\n".join(parts)

    # ── list_knowledge_bases ──────────────────────────────────────────

    async def _list_knowledge_bases() -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.knowledge_base import KnowledgeBase

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []

            q = select(KnowledgeBase)
            q = q.where(caller_visible_filter(KnowledgeBase, uid, group_ids))
            q = q.order_by(KnowledgeBase.name)

            result = await _db.execute(q)
            kbs = result.scalars().all()

            if not kbs:
                return "No knowledge bases found."

            parts: list[str] = [f"Found {len(kbs)} knowledge base(s):\n"]
            for kb in kbs:
                cm = " [company-managed]" if kb.is_company_managed else ""
                parts.append(
                    f"• **{kb.name}** (id: `{kb.id}`, docs: "
                    f"{kb.document_count}, chunks: {kb.chunk_count}){cm}\n"
                    f"  {kb.description or 'No description'}"
                )
            return "\n".join(parts)

    # ── create_knowledge_base ─────────────────────────────────────────

    async def _create_knowledge_base(
        name: str, description: str = "", is_company_managed: bool = False
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.models.knowledge_base import KnowledgeBase

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Knowledge base creation is not available for anonymous users."
        # Canonicalise the name up front so the dedup pre-check and the stored
        # row use the stripped form; reject a whitespace-only name.
        name = (name or "").strip()
        if not name:
            return (
                "Knowledge base name is required (whitespace-only names are not "
                "allowed)."
            )

        # Friendly tool message used for both the pre-check hit and the
        # post-IntegrityError race-window recovery — same shape so the LLM
        # gets a consistent response regardless of which branch matched.
        def _existing_kb_message(existing: KnowledgeBase) -> str:
            return (
                f"Knowledge base **{existing.name}** already exists.\n"
                f"  • ID: `{existing.id}`\n"
                "Use this existing knowledge base to add documents."
            )

        _failed_message = "Failed to create knowledge base. Please try again."

        # The outer ``try`` deliberately wraps the ``async with`` so that
        # exceptions raised during session acquisition itself
        # (``__aenter__``: pool exhaustion, connection refused, etc.) or
        # session teardown (``__aexit__``: close-on-exit failures, driver
        # bugs) are also converted to the friendly ``_failed_message``
        # instead of leaking raw exception strings to the LLM through
        # ``CallableToolExecutor``. The inner ``try`` inside the
        # ``async with`` block handles in-session DB errors (and rolls
        # back); the outer ``try`` handles everything else.
        #
        # ``session_acquired`` lets the outer handler distinguish the two
        # cases so the log message stays accurate for on-call diagnosis:
        # an acquisition failure is usually a pool / connectivity issue,
        # while a teardown failure is usually a driver-level close error
        # *after* the in-session work already ran. Both still surface the
        # same friendly tool message to the LLM.
        session_acquired = False
        try:
            async with _builder_session() as _db:
                session_acquired = True
                # Creating a company-managed (platform-wide) KB is admin-only,
                # mirroring the REST create endpoint and the other create_*
                # builder tools (create_agent, create_prompt, ...).
                if is_company_managed:
                    caller = await _load_caller_user_for_builder(_db, uid)
                    if caller is None or not caller.is_admin:
                        return (
                            "Only admins can create company-managed knowledge "
                            "bases."
                        )
                try:
                    # Best-effort pre-check: handles the common case where
                    # the LLM retries the same ``create_knowledge_base``
                    # call later in the same conversation. This is **not**
                    # a concurrency guarantee — the ``KnowledgeBase`` model
                    # intentionally allows duplicate ``(user_id, name)``
                    # pairs elsewhere on the platform (the KB REST API
                    # does not enforce uniqueness either), so adding a
                    # DB-level unique constraint would change behaviour
                    # for unrelated callers. Two concurrent agent-builder
                    # sessions inserting the same name can still both
                    # succeed; that's accepted by design.
                    # ``scalars().first()`` (not ``scalar_one_or_none()``)
                    # because the comment block above explicitly accepts
                    # duplicate ``(user_id, name)`` pairs as a possible
                    # pre-existing state — ``scalar_one_or_none`` would
                    # raise ``MultipleResultsFound`` in that case and turn
                    # a usable "you already have one of these" answer into
                    # a generic failure message.
                    # ``order_by(created_at, id).limit(1)`` keeps the
                    # chosen "already exists" KB deterministic when the
                    # user has multiple matching rows — without it,
                    # ``.first()`` on an unordered query can return
                    # different rows across calls and steer the LLM to
                    # different KB IDs over time.
                    existing_result = await _db.execute(
                        sa_select(KnowledgeBase)
                        .where(
                            KnowledgeBase.user_id == uid,
                            KnowledgeBase.name == name,
                        )
                        .order_by(KnowledgeBase.created_at, KnowledgeBase.id)
                        .limit(1)
                    )
                    existing = existing_result.scalars().first()
                    if existing:
                        return _existing_kb_message(existing)

                    kb = KnowledgeBase(
                        user_id=uid,
                        name=name,
                        description=(description or "").strip() or None,
                        is_company_managed=is_company_managed,
                    )
                    _db.add(kb)
                    try:
                        await _db.commit()
                    except IntegrityError:
                        # Defensive scaffolding: a unique-constraint
                        # IntegrityError on ``(user_id, name)`` is not
                        # possible against the current schema (see the
                        # comment block above — no such constraint exists
                        # today). Other IntegrityError causes (PK collision
                        # on ``id``, FK violations, etc.) can still fire,
                        # and this branch handles them defensively: if a
                        # partial unique index on ``(user_id, name)`` is
                        # ever added the race-window re-check + rollback
                        # path is already wired up, and any other
                        # IntegrityError flavour is still handled in this
                        # ``except`` block via ``logger.exception`` plus
                        # the friendly failure return below. The
                        # IntegrityError must not surface to the LLM as a
                        # raw exception.
                        await _db.rollback()
                        try:
                            existing_result = await _db.execute(
                                sa_select(KnowledgeBase)
                                .where(
                                    KnowledgeBase.user_id == uid,
                                    KnowledgeBase.name == name,
                                )
                                .order_by(KnowledgeBase.created_at, KnowledgeBase.id)
                                .limit(1)
                            )
                            # ``scalars().first()`` for the same reason
                            # as the pre-check above — tolerate
                            # pre-existing duplicates instead of crashing
                            # on them. The ``order_by(created_at,
                            # id).limit(1)`` mirrors the pre-check so
                            # both branches return the same row when
                            # duplicates already exist.
                            existing = existing_result.scalars().first()
                        except Exception:
                            logger.exception(
                                "Agent builder create_knowledge_base race-window "
                                "re-check failed for user %s",
                                uid,
                            )
                            return _failed_message
                        if existing:
                            return _existing_kb_message(existing)
                        logger.exception(
                            "Agent builder create_knowledge_base integrity error "
                            "for user %s without race-window match",
                            uid,
                        )
                        return _failed_message
                    return (
                        f"Knowledge base **{name}** created successfully!\n"
                        f"  • ID: `{kb.id}`\n"
                        "You can now add documents with `add_kb_document`."
                    )
                except Exception:
                    # In-session guard: any other exception (transient DB
                    # error during the pre-check, network blip, etc.) is
                    # converted to the same friendly message. ``rollback``
                    # is itself wrapped because rolling back an
                    # already-broken transaction can raise — and we'd
                    # rather lose the rollback than the friendly response
                    # to the LLM.
                    with contextlib.suppress(Exception):
                        await _db.rollback()
                    logger.exception("Agent builder create_knowledge_base failed")
                    return _failed_message
        except Exception:
            # Last-chance guard for session acquisition / teardown
            # failures (``async with`` __aenter__ / __aexit__ raising —
            # pool exhaustion, connection refused, driver bugs, close-
            # on-exit errors). There is no usable ``_db`` to roll back
            # here, so we just log and return the friendly message.
            # Without this outer guard those exceptions would bypass the
            # in-session handler and surface to the LLM as raw exception
            # strings via ``CallableToolExecutor``.
            #
            # Distinguish acquire vs teardown for on-call: if
            # ``session_acquired`` was never flipped, ``__aenter__``
            # raised (no session was ever usable); otherwise the
            # in-session block already ran and the failure is during
            # ``__aexit__``.
            if session_acquired:
                logger.exception(
                    "Agent builder create_knowledge_base session teardown failed"
                )
            else:
                logger.exception(
                    "Agent builder create_knowledge_base failed to acquire session"
                )
            return _failed_message

    # ── add_kb_document ───────────────────────────────────────────────

    async def _add_kb_document(knowledge_base_id: str, title: str, content: str) -> str:
        from app.api.v1.helpers import (
            get_user_group_ids,
            user_visible_filter,
        )
        from app.core.database import async_session as _builder_session
        from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
        from app.services.kb_helpers import (
            bump_kb_revision,
            enqueue_document_processing,
        )

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Adding documents is not available for anonymous users."

        kb_uuid, id_err = _validate_id_arg(knowledge_base_id, "knowledge_base_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            # Resolve the caller first so the KB SELECT can apply the
            # visibility filter with the correct admin bypass.  The
            # "load caller before KB" ordering mirrors
            # ``_load_writable_agent_for_builder`` /
            # ``_load_writable_assignment_for_builder``: without the
            # caller we cannot tell whether ``user_visible_filter``
            # should bypass on admin role, and an unfiltered SELECT
            # followed by an admin-aware permission check would leak
            # the existence of private KBs owned by other users (same
            # bug class as for agents/assignments).
            caller = await _load_caller_user_for_builder(_db, uid)
            if caller is None:
                return "User not found."
            group_ids = await get_user_group_ids(_db, uid)

            kb_result = await _db.execute(
                sa_select(KnowledgeBase).where(
                    KnowledgeBase.id == kb_uuid,
                    user_visible_filter(
                        KnowledgeBase, uid, group_ids, is_admin=agent_tool_admin_bypass(caller)
                    ),
                )
            )
            kb = kb_result.scalar_one_or_none()
            if not kb:
                # Existence-hiding by design (matches
                # ``get_user_knowledge_base``): covers both "row does
                # not exist" and "row exists but is invisible to the
                # caller" so a probe cannot distinguish them.
                return (
                    f"Knowledge base `{knowledge_base_id}` not found or "
                    "you don't have access."
                )

            # The REST add-document gate, locks and caller rights alike:
            # package-installed, built-in, company-managed (admin only),
            # share tier, then read-only.
            if write_err := await writable_knowledge_base_error(
                _db,
                kb,
                caller,
                group_ids,
                action="add documents to",
                content_action="add documents to it",
            ):
                return _as_sentence(write_err)

            try:
                doc = KnowledgeDocument(
                    knowledge_base_id=kb.id,
                    filename=title,
                    source_type="text",
                    content=content,
                    status="pending",
                )
                _db.add(doc)
                kb.document_count = (kb.document_count or 0) + 1
                await bump_kb_revision(_db, kb.id)
                await _db.commit()
                await enqueue_document_processing(str(doc.id))
                return (
                    f"Document **{title}** added to knowledge base "
                    f"**{kb.name}**.\n"
                    f"  • Document ID: `{doc.id}`\n"
                    "  • Status: pending (processing will start shortly)"
                )
            except Exception as e:
                logger.error(
                    "Agent builder add_kb_document failed: %s",
                    format_exception_message(e),
                    exc_info=True,
                )
                return "Failed to add document. Please try again."

    # ── list_custom_tools ─────────────────────────────────────────────

    async def _list_custom_tools() -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.custom_tool import CustomTool

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []

            q = select(CustomTool)
            q = q.where(caller_visible_filter(CustomTool, uid, group_ids))
            q = q.order_by(CustomTool.name)

            result = await _db.execute(q)
            tools = result.scalars().all()

            if not tools:
                return "No custom tools found."

            parts: list[str] = [f"Found {len(tools)} custom tool(s):\n"]
            for ct in tools:
                cm = " [company-managed]" if ct.is_company_managed else ""
                enabled = "enabled" if ct.enabled else "disabled"
                parts.append(
                    f"• **{ct.name}** (id: `{ct.id}`, {enabled}){cm}\n"
                    f"  {ct.description or 'No description'}"
                )
            return "\n".join(parts)

    # ── get_custom_tool ───────────────────────────────────────────────

    async def _get_custom_tool(tool_id: str) -> str:
        import json as json_mod

        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.custom_tool import CustomTool

        tool_uuid, id_err = _validate_id_arg(tool_id, "tool_id")
        if id_err:
            return id_err

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []

            q = select(CustomTool).where(CustomTool.id == tool_uuid)
            q = q.where(caller_visible_filter(CustomTool, uid, group_ids))

            result = await _db.execute(q)
            ct = result.scalar_one_or_none()
            if not ct:
                return (
                    f"Custom tool `{tool_id}` not found or you don't " "have access."
                )

            schema_str = (
                json_mod.dumps(ct.input_schema, indent=2) if ct.input_schema else "None"
            )
            parts = [
                f"**{ct.name}**",
                f"  • ID: `{ct.id}`",
                f"  • Description: {ct.description or 'None'}",
                f"  • Language: {ct.language}",
                f"  • Enabled: {'Yes' if ct.enabled else 'No'}",
                f"  • Input schema: {schema_str}",
                f"  • Company-managed: {'Yes' if ct.is_company_managed else 'No'}",
            ]
            return "\n".join(parts)

    # ── list_integrations ─────────────────────────────────────────────

    async def _list_integrations() -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.integration import Integration

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []

            q = select(Integration)
            q = q.where(caller_visible_filter(Integration, uid, group_ids))
            q = q.order_by(Integration.name)

            result = await _db.execute(q)
            integrations = result.scalars().all()

            if not integrations:
                return "No integrations found."

            from app.services.integration_credentials import PER_USER_CREDENTIAL_MODE
            from app.services.integration_user_credentials import connected_user_counts

            # On a per_user OAuth row, how many people connected their own
            # account: one grouped count for the page, never who.
            per_user_ids = [
                i.id
                for i in integrations
                if i.auth_type == "oauth"
                and getattr(i, "credential_mode", None) == PER_USER_CREDENTIAL_MODE
            ]
            connected = await connected_user_counts(_db, per_user_ids) if per_user_ids else {}
            # Which way each A2A partner relationship points: one grouped read.
            from app.a2a.providers import ALL_A2A_PROVIDERS, directions_of

            a2a_ids = [i.id for i in integrations if i.provider in ALL_A2A_PROVIDERS]
            directions = await directions_of(_db, a2a_ids) if a2a_ids else {}

            parts: list[str] = [f"Found {len(integrations)} integration(s):\n"]
            for i in integrations:
                cm = " [company-managed]" if i.is_company_managed else ""
                # The suffix its tools carry when an agent binds several
                # integrations of this provider; stable across renames.
                suffix = f", tool suffix: `__{i.tool_slug}`" if i.tool_slug else ""
                mode = ""
                if i.auth_type == "oauth":
                    mode = ", credential mode: " + (
                        f"per_user, {connected.get(str(i.id), 0)} connected their own account"
                        if getattr(i, "credential_mode", None) == PER_USER_CREDENTIAL_MODE
                        else "shared"
                    )
                if i.provider in ALL_A2A_PROVIDERS:
                    mode += f", A2A direction: {directions.get(str(i.id), 'none')}"
                parts.append(
                    f"• **{i.name}** (id: `{i.id}`, provider: {i.provider}, "
                    f"status: {i.status}{suffix}{mode}){cm}\n"
                    f"  {i.description or 'No description'}"
                )
            return "\n".join(parts)

    # ── Prompts ───────────────────────────────────────────────────────

    async def _list_prompts(
        search: str = "", compact: bool = False, limit: object = None, offset: object = None
    ) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.prompt import Prompt

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            q = select(Prompt)
            q = q.where(caller_visible_filter(Prompt, uid, group_ids))
            q = q.order_by(Prompt.name)
            result = await _db.execute(q)
            prompts = result.scalars().all()
            rows: list[tuple[str, str, str, str]] = []
            for p in prompts:
                cm = " [company-managed]" if p.is_company_managed else ""
                rows.append((
                    p.name,
                    p.description or "",
                    f"• **{p.name}** (id: `{p.id}`){cm}\n  {p.description or 'No description'}",
                    f"{p.id} | {p.name}{cm}",
                ))
            return _catalog_list_page(
                rows, noun="prompt", search=search, compact=compact, limit=limit, offset=offset
            )

    async def _get_prompt(prompt_id: str) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.prompt import Prompt

        prompt_uuid, id_err = _validate_id_arg(prompt_id, "prompt_id")
        if id_err:
            return id_err
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            q = select(Prompt).where(Prompt.id == prompt_uuid)
            q = q.where(caller_visible_filter(Prompt, uid, group_ids))
            result = await _db.execute(q)
            p = result.scalar_one_or_none()
            if not p:
                return f"Prompt `{prompt_id}` not found or you don't have access."
            parts = [
                f"**{p.name}**",
                f"  • ID: `{p.id}`",
                f"  • Description: {p.description or 'None'}",
                f"  • Tags: {', '.join(p.tags) if p.tags else 'None'}",
                f"  • Hidden from chat: {'Yes' if p.hide_from_chat else 'No'}",
                f"  • Company-managed: {'Yes' if p.is_company_managed else 'No'}",
                *await _catalog_access_lines(_db, p),
            ]
            # Surface the content in FULL (never truncated) so the agent can
            # faithfully inspect and rewrite it (same rationale as get_agent /
            # get_assignment); the agent loop caps oversize results.
            parts.append("")
            parts.append("Content (full):")
            parts.append(p.content or "None")
            return "\n".join(parts)

    async def _create_prompt(
        name: str,
        content: str,
        description: str = "",
        tags: str = "",
        hide_from_chat: bool = False,
        is_company_managed: bool = False,
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.models.prompt import Prompt
        from app.services.prompt_versioning import create_prompt_version

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Prompt creation is not available for anonymous users."
        name = (name or "").strip()
        if not name:
            return "Prompt name is required (whitespace-only names are not allowed)."

        async with _builder_session() as _db:
            caller = await _load_caller_user_for_builder(_db, uid)
            if caller is None:
                return "User not found."
            if is_company_managed and not caller.is_admin:
                return "Only admins can create company-managed prompts."
            tag_list = (
                [t.strip() for t in tags.split(",") if t.strip()] if tags else []
            )
            prompt = Prompt(
                user_id=uid,
                name=name,
                description=(description or "").strip(),
                content=content or "",
                tags=tag_list,
                hide_from_chat=hide_from_chat,
                is_company_managed=is_company_managed,
            )
            _db.add(prompt)
            try:
                await _db.flush()
                await create_prompt_version(
                    _db,
                    prompt_id=prompt.id,
                    content=prompt.content,
                    author_id=uid,
                    commit_message="Initial version",
                )
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder create_prompt failed for user %s", uid
                )
                await _db.rollback()
                return "Failed to create prompt. Please try again."
            return (
                f"Prompt **{name}** created successfully!\n"
                f"  • ID: `{prompt.id}`\n"
                "Attach it to an agent with `update_agent` (prompt_ids)."
            )

    async def _update_prompt(
        prompt_id: str,
        name: str = "",
        content: str = "",
        description: str = "",
        tags: str = "",
        hide_from_chat: str = "",
        owner: str = "",
        users: object = None,
        users_permission: str = "",
        shared_group_ids: object = None,
        shared_group_permissions: object = None,
        binding_mode: str = "",
        mode: str = "replace",
        section: str = "",
        edits: list[dict[str, object]] | None = None,
        confirm_shrink: bool = False,
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.models.prompt import Prompt
        from app.services.prompt_versioning import create_prompt_version
        from app.services.sandbox_workspace.editing import unified_diff
        from app.services.skill_edits import SKILL_UPDATE_MODES, SkillEditError, new_skill_content

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Prompt updates are not available for anonymous users."
        prompt_uuid, id_err = _validate_id_arg(prompt_id, "prompt_id")
        if id_err:
            return id_err
        # The content modes of update_skill, through the same helper: a
        # partial mode changes part of the stored text and answers its diff,
        # a whole replace of a long prompt is refused when it would wipe it.
        mode = (mode or "replace").strip().lower() if isinstance(mode, str) else "replace"
        if mode not in SKILL_UPDATE_MODES:
            return f"Error: unknown mode '{mode}': use one of {', '.join(SKILL_UPDATE_MODES)}. Nothing was changed."
        if mode == "edit" and (content or "").strip():
            return "Error: mode 'edit' takes 'edits', not 'content'. Nothing was changed."

        async with _builder_session() as _db:
            prompt, perm_err = await _load_manageable_entity_for_builder(
                _db, Prompt, uid, prompt_uuid, action="edit", entity_label="prompt"
            )
            if perm_err is not None or prompt is None:
                return perm_err or f"Prompt `{prompt_id}` not found."

            updated: list[str] = []
            content_changed = False
            # Free-text fields: whitespace-only = "leave unchanged", stored
            # value is the stripped form (consistent across builder updates).
            if (name or "").strip():
                prompt.name = (name or "").strip()
                updated.append("name")
            if (description or "").strip():
                prompt.description = (description or "").strip()
                updated.append("description")
            detail = ""
            new_content: str | None = None
            if mode != "replace" or (content and content != prompt.content):
                # Only treat content as changed when it actually differs, so an
                # identical-content call doesn't report "updated: content",
                # create a spurious new version, or trigger a needless commit.
                try:
                    new_content, detail = new_skill_content(
                        mode,
                        prompt.content or "",
                        content=content or "",
                        section=section,
                        edits=edits,
                        confirm_shrink=bool(confirm_shrink),
                        noun="prompt",
                    )
                except SkillEditError as exc:
                    message = str(exc).rstrip()
                    if not message.endswith("Nothing was changed."):
                        message = f"{message} Nothing was changed."
                    return f"Error: {message}"
                if mode != "replace" and new_content == (prompt.content or ""):
                    return f"Error: this {mode} leaves the prompt content unchanged. Nothing was changed."
            if new_content is not None:
                if mode != "replace":
                    diff = unified_diff("content", prompt.content or "", new_content)
                    detail = f"{detail}\n```diff\n{diff}```"
                prompt.content = new_content
                content_changed = True
                updated.append("content" if mode == "replace" else f"content ({mode})")
            if tags:
                prompt.tags = [t.strip() for t in tags.split(",") if t.strip()]
                updated.append("tags")
            if hide_from_chat and hide_from_chat.lower() in ("true", "false"):
                prompt.hide_from_chat = hide_from_chat.lower() == "true"
                updated.append("hide_from_chat")

            caller = await _load_caller_user_for_builder(_db, uid) if _arg_provided(owner) else None
            access_updated, access_lines, access_err = await _apply_catalog_access(
                _db,
                prompt,
                noun="prompt",
                entity_type="prompt",
                caller=caller,
                owner=owner,
                users=users,
                users_permission=users_permission,
                shared_group_ids=shared_group_ids,
                shared_group_permissions=shared_group_permissions,
                binding_mode=binding_mode,
            )
            if access_err:
                await _db.rollback()
                return access_err
            updated.extend(access_updated)
            access_note = (" " + " ".join(access_lines)) if access_lines else ""
            if not updated:
                if access_lines:
                    return f"Nothing changed on prompt **{prompt.name}**:{access_note}"
                return "No fields to update. Provide at least one field."
            try:
                if content_changed:
                    await create_prompt_version(
                        _db,
                        prompt_id=prompt.id,
                        content=prompt.content,
                        author_id=uid,
                        commit_message=None if mode == "replace" else f"update_prompt ({mode})",
                    )
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder update_prompt failed for prompt %s", prompt_id
                )
                await _db.rollback()
                return f"Failed to update prompt `{prompt_id}`. Please try again."
            return f"Prompt **{prompt.name}** updated: {', '.join(updated)}.{detail}{access_note}"

    async def _delete_prompt(prompt_id: str) -> str:
        from sqlalchemy import select

        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.prompt import Prompt

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Prompt deletion is not available for anonymous users."
        prompt_uuid, id_err = _validate_id_arg(prompt_id, "prompt_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            prompt, perm_err = await _load_manageable_entity_for_builder(
                _db, Prompt, uid, prompt_uuid, action="delete", entity_label="prompt"
            )
            if perm_err is not None or prompt is None:
                return perm_err or f"Prompt `{prompt_id}` not found."
            p_name = prompt.name
            pid = str(prompt.id)
            # Purge stale references from all agents' prompt_ids.
            agents_result = await _db.execute(select(Agent))
            for agent in agents_result.scalars().all():
                if agent.prompt_ids and pid in agent.prompt_ids:
                    agent.prompt_ids = [v for v in agent.prompt_ids if v != pid]
            await _db.delete(prompt)
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder delete_prompt failed for prompt %s", prompt_id
                )
                await _db.rollback()
                return f"Failed to delete prompt `{prompt_id}`. Please try again."
            return f"Prompt **{p_name}** has been deleted."

    # ── Skills ────────────────────────────────────────────────────────

    async def _list_skills(
        search: str = "", compact: bool = False, limit: object = None, offset: object = None
    ) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.agent_skill import AgentSkill

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            q = select(AgentSkill)
            q = q.where(caller_visible_filter(AgentSkill, uid, group_ids))
            q = q.order_by(AgentSkill.name)
            result = await _db.execute(q)
            skills = result.scalars().all()
            rows: list[tuple[str, str, str, str]] = []
            for s in skills:
                cm = " [company-managed]" if s.is_company_managed else ""
                src = "URL" if s.url else "inline"
                state = "enabled" if s.enabled is not False else "disabled"
                rows.append((
                    s.name,
                    s.description or "",
                    f"• **{s.name}** (id: `{s.id}`, source: {src}){cm}\n"
                    f"  {s.description or 'No description'}",
                    f"{s.id} | {s.name} | {state}{cm}",
                ))
            return _catalog_list_page(
                rows, noun="skill", search=search, compact=compact, limit=limit, offset=offset
            )

    async def _get_skill(skill_id: str, outline: bool = False, section: str = "") -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.agent_skill import AgentSkill
        from app.services.skill_edits import SkillEditError, format_outline, resolve_section

        skill_uuid, id_err = await _id_arg(skill_id, "skill_id")
        if id_err:
            return id_err
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            q = select(AgentSkill).where(AgentSkill.id == skill_uuid)
            q = q.where(caller_visible_filter(AgentSkill, uid, group_ids))
            result = await _db.execute(q)
            s = result.scalar_one_or_none()
            if not s:
                return f"Skill `{skill_id}` not found or you don't have access."
            parts = [
                f"**{s.name}**",
                f"  • ID: `{s.id}`",
                f"  • URL: {s.url or 'None (inline content)'}",
                f"  • Description: {s.description or 'None'}",
                f"  • Compatibility: {s.compatibility or 'None'}",
                f"  • Enabled: {'Yes' if s.enabled else 'No'}",
                f"  • Company-managed: {'Yes' if s.is_company_managed else 'No'}",
                *await _catalog_access_lines(_db, s),
            ]
            body = s.content or ""
            wanted = (section or "").strip() if isinstance(section, str) else str(section or "")
            if outline or wanted:
                # Outline / one section: a long skill is read and changed
                # by section instead of whole, like get_knowledge_document.
                parts.append(f"  • Content: {len(body)} characters")
                parts.append("")
                if outline:
                    parts.append(format_outline(body))
                if wanted:
                    try:
                        found = resolve_section(body, wanted)
                    except SkillEditError as exc:
                        parts.append(f"Error: {exc}")
                    else:
                        if outline:
                            parts.append("")
                        parts.append(
                            f"Section '{found.title}' (characters {found.start}-{found.end}, verbatim; "
                            "change it alone with update_skill mode 'replace_section'):"
                        )
                        parts.append(body[found.start : found.end])
                return "\n".join(parts)
            # Surface the content in FULL (never truncated) so the agent can
            # faithfully inspect and rewrite it (same rationale as get_agent /
            # get_assignment); the agent loop caps oversize results.
            parts.append("")
            parts.append("Content (full):")
            parts.append(s.content or "None")
            return "\n".join(parts)

    async def _create_skill(
        name: str = "",
        url: str = "",
        content: str = "",
        description: str = "",
        compatibility: str = "",
        tags: str = "",
        hide_from_chat: bool = False,
        is_company_managed: bool = False,
    ) -> str:
        from datetime import UTC, datetime

        from fastapi import HTTPException

        from app.core.database import async_session as _builder_session
        from app.models.agent_skill import AgentSkill
        from app.services.skill_source import fetch_skill_content, parse_skill_frontmatter

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Skill creation is not available for anonymous users."
        # Canonicalise ``url`` up front so a whitespace-only value (e.g.
        # ``url="   "``) is treated as "not provided" rather than slipping past
        # the checks below and failing later with an opaque fetch/SSRF error;
        # the stripped form is what gets fetched and stored. ``content`` is left
        # as-is apart from the emptiness check (its body may legitimately have
        # leading/trailing whitespace).
        url = (url or "").strip()
        if not url and not (content or "").strip():
            return "Provide either a 'url' (to a SKILL.md) or 'content' (markdown)."
        if url and (content or "").strip():
            # Mutually exclusive — same contract as ``update_skill`` — so the
            # caller picks a single source of truth rather than silently
            # ignoring ``content`` in favour of ``url``. A whitespace-only
            # ``content`` is treated as "not provided" (consistent with the
            # emptiness check above), so ``url`` + padded ``content`` is not
            # spuriously rejected as "both".
            return (
                "Provide either 'url' (to fetch a SKILL.md) or 'content' (inline "
                "markdown), not both."
            )

        raw_content: str | None = None
        meta: dict[str, str] = {}
        if url:
            try:
                raw_content, meta = await fetch_skill_content(url)
            except Exception as exc:
                # Surface a friendly hint instead of a raw traceback.
                return f"Failed to fetch skill from URL: {exc}"
        else:
            raw_content = content
            # Enforce the same size guard as the REST API / URL path: a
            # paste-mode body over the 1 MB limit must be rejected, not silently
            # persisted (``parse_skill_frontmatter`` raises ``HTTPException`` for
            # oversize content). Other (non-size) parse issues are tolerated —
            # the skill is still created, just without backfilled metadata.
            try:
                meta, _ = parse_skill_frontmatter(raw_content)
            except HTTPException as exc:
                return f"Failed to create skill: {exc.detail}"
            except Exception:
                meta = {}

        resolved_name = (name or "").strip() or meta.get("name")
        if not resolved_name:
            resolved_name = (
                url.rsplit("/", 1)[-1].replace(".md", "") if url else "Untitled Skill"
            )

        async with _builder_session() as _db:
            caller = await _load_caller_user_for_builder(_db, uid)
            if caller is None:
                return "User not found."
            if is_company_managed and not caller.is_admin:
                return "Only admins can create company-managed skills."
            tag_list = (
                [t.strip() for t in tags.split(",") if t.strip()] if tags else []
            )
            skill = AgentSkill(
                user_id=uid,
                url=url or None,
                name=resolved_name,
                description=description or meta.get("description"),
                compatibility=compatibility or meta.get("compatibility"),
                content=raw_content,
                fetched_at=datetime.now(UTC) if url else None,
                tags=tag_list,
                hide_from_chat=hide_from_chat,
                is_company_managed=is_company_managed,
            )
            _db.add(skill)
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder create_skill failed for user %s", uid
                )
                await _db.rollback()
                return "Failed to create skill. Please try again."
            return (
                f"Skill **{skill.name}** created successfully!\n"
                f"  • ID: `{skill.id}`\n"
                "Attach it to an agent with `update_agent` (skill_ids)."
            )

    async def _update_skill(
        skill_id: str,
        name: str = "",
        content: str = "",
        url: str = "",
        description: str = "",
        compatibility: str = "",
        tags: str = "",
        enabled: str = "",
        hide_from_chat: str = "",
        mode: str = "replace",
        section: str = "",
        edits: list[dict[str, object]] | None = None,
        confirm_shrink: bool = False,
        owner: str = "",
        users: object = None,
        users_permission: str = "",
        shared_group_ids: object = None,
        shared_group_permissions: object = None,
        binding_mode: str = "",
    ) -> str:
        from datetime import UTC, datetime

        from fastapi import HTTPException

        from app.core.database import async_session as _builder_session
        from app.models.agent_skill import AgentSkill
        from app.services.skill_edits import SKILL_UPDATE_MODES, SkillEditError, new_skill_content
        from app.services.skill_source import fetch_skill_content, parse_skill_frontmatter
        from app.services.skill_versioning import create_skill_version

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Skill updates are not available for anonymous users."
        skill_uuid, id_err = await _id_arg(skill_id, "skill_id")
        if id_err:
            return id_err
        mode = (mode or "replace").strip().lower() if isinstance(mode, str) else "replace"
        if mode not in SKILL_UPDATE_MODES:
            return f"Error: unknown mode '{mode}': use one of {', '.join(SKILL_UPDATE_MODES)}. Nothing was changed."
        partial = mode != "replace"
        if partial and (url or "").strip():
            return f"Error: 'url' is accepted with mode 'replace' only, not '{mode}'. Nothing was changed."
        if mode == "edit" and (content or "").strip():
            return "Error: mode 'edit' takes 'edits', not 'content'. Nothing was changed."
        # Canonicalise ``url`` up front: a whitespace-only value (e.g. "   ")
        # is treated as "leave unchanged", and a padded URL is stored in its
        # stripped form rather than triggering an unnecessary re-fetch / SSRF
        # validation against the untrimmed value.
        url = (url or "").strip()
        # A whitespace-only ``content`` is treated as "not provided" everywhere
        # below (consistent with ``create_skill`` and the rest of the builder),
        # so ``url`` + padded ``content`` is not spuriously rejected as "both"
        # and can't later undo a URL re-fetch by storing a blank body. A real
        # body is still stored verbatim (its internal/trailing whitespace is
        # meaningful) via the unstripped ``content`` in the storage block.
        if url and (content or "").strip():
            return (
                "Provide either 'url' (to re-fetch a SKILL.md) or 'content' "
                "(inline markdown), not both."
            )
        if (content or "").strip():
            # Enforce the same 1 MB size guard as the REST API before storing
            # inline content (``parse_skill_frontmatter`` raises ``HTTPException`` for
            # oversize content; metadata is ignored here). Validated up front so
            # an oversized body is rejected before any mutation/commit. Non-size
            # parse issues are tolerated — the content is stored as-is below.
            try:
                parse_skill_frontmatter(content)
            except HTTPException as exc:
                return f"Failed to update skill: {exc.detail}"
            except Exception:
                logger.debug("update_skill frontmatter parse failed (ignored)")

        async with _builder_session() as _db:
            skill, perm_err = await _load_manageable_entity_for_builder(
                _db, AgentSkill, uid, skill_uuid, action="edit", entity_label="skill"
            )
            if perm_err is not None or skill is None:
                return perm_err or f"Skill `{skill_id}` not found."
            if skill.tags and "builtin" in skill.tags:
                return "Built-in skills cannot be modified."

            previous_content = getattr(skill, "content", None)
            previous_description = getattr(skill, "description", None)
            previous_compatibility = getattr(skill, "compatibility", None)
            detail = ""
            new_content: str | None = None
            if partial or (content or "").strip():
                # Every content write goes through one path: the section
                # and edit modes change one part, and a whole-body replace is
                # refused when it would wipe most of a long skill.
                try:
                    new_content, detail = new_skill_content(
                        mode,
                        previous_content or "",
                        content=content or "",
                        section=section,
                        edits=edits,
                        confirm_shrink=bool(confirm_shrink),
                    )
                except SkillEditError as exc:
                    message = str(exc)
                    if not message.rstrip().endswith("Nothing was changed."):
                        message = f"{message} Nothing was changed."
                    return f"Error: {message}"
                if partial:
                    try:
                        parse_skill_frontmatter(new_content)
                    except HTTPException as exc:
                        return f"Failed to update skill: {exc.detail}"
                    except Exception:
                        logger.debug("update_skill frontmatter parse failed (ignored)")

            updated: list[str] = []
            if url and url != skill.url:
                # Re-point the skill at a new SKILL.md and re-fetch its content,
                # mirroring the REST ``PUT /agent-skills/{id}`` behaviour.  Falls
                # back to a friendly message if the fetch fails.
                try:
                    raw_content, meta = await fetch_skill_content(url)
                except Exception as exc:
                    return f"Failed to fetch skill from URL: {exc}"
                skill.url = url
                skill.content = raw_content
                skill.fetched_at = datetime.now(UTC)
                # Backfill metadata from the new frontmatter unless explicitly
                # provided in this same call.
                if not (name or "").strip() and meta.get("name"):
                    skill.name = meta["name"]
                if not (description or "").strip() and meta.get("description"):
                    skill.description = meta["description"]
                if not (compatibility or "").strip() and meta.get("compatibility"):
                    skill.compatibility = meta["compatibility"]
                updated.append("url")
            if new_content is not None:
                # Inline content replaces the cached body and detaches the URL.
                # Stored verbatim (a real body's internal/trailing whitespace is
                # meaningful); only a whitespace-only value is ignored above.
                skill.content = new_content
                skill.url = None
                skill.fetched_at = None
                updated.append("content" if mode == "replace" else f"content ({mode})")
            # Free-text fields: a whitespace-only value is "leave unchanged",
            # the stored value is the stripped form (consistent with the rest of
            # the builder's update tools).
            if (name or "").strip():
                skill.name = (name or "").strip()
                updated.append("name")
            if (description or "").strip():
                skill.description = (description or "").strip()
                updated.append("description")
            if (compatibility or "").strip():
                skill.compatibility = (compatibility or "").strip()
                updated.append("compatibility")
            if tags:
                skill.tags = [t.strip() for t in tags.split(",") if t.strip()]
                updated.append("tags")
            if enabled and enabled.lower() in ("true", "false"):
                skill.enabled = enabled.lower() == "true"
                updated.append("enabled")
            if hide_from_chat and hide_from_chat.lower() in ("true", "false"):
                skill.hide_from_chat = hide_from_chat.lower() == "true"
                updated.append("hide_from_chat")

            caller = await _load_caller_user_for_builder(_db, uid) if _arg_provided(owner) else None
            access_updated, access_lines, access_err = await _apply_catalog_access(
                _db,
                skill,
                noun="skill",
                entity_type="agent_skill",
                caller=caller,
                owner=owner,
                users=users,
                users_permission=users_permission,
                shared_group_ids=shared_group_ids,
                shared_group_permissions=shared_group_permissions,
                binding_mode=binding_mode,
            )
            if access_err:
                await _db.rollback()
                return access_err
            updated.extend(access_updated)
            access_note = (" " + " ".join(access_lines)) if access_lines else ""
            if not updated:
                if access_lines:
                    return f"Nothing changed on skill **{skill.name}**:{access_note}"
                return "No fields to update. Provide at least one field."
            try:
                if getattr(skill, "content", None) is not None and (
                    skill.content != previous_content
                    or getattr(skill, "description", None) != previous_description
                    or getattr(skill, "compatibility", None) != previous_compatibility
                ):
                    # A version per change, as the REST path writes: a
                    # bad write is rolled back from the skill's history.
                    await create_skill_version(
                        _db, skill=skill, author_id=uid, commit_message=f"update_skill ({mode})"
                    )
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder update_skill failed for skill %s", skill_id
                )
                await _db.rollback()
                return f"Failed to update skill `{skill_id}`. Please try again."
            size = f" Content: {len(skill.content)} characters." if new_content is not None and skill.content is not None else ""
            return f"Skill **{skill.name}** updated: {', '.join(updated)}.{detail}{size}{access_note}"

    async def _delete_skill(skill_id: str) -> str:
        from sqlalchemy import select

        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.agent_skill import AgentSkill

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Skill deletion is not available for anonymous users."
        skill_uuid, id_err = _validate_id_arg(skill_id, "skill_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            skill, perm_err = await _load_manageable_entity_for_builder(
                _db, AgentSkill, uid, skill_uuid, action="delete", entity_label="skill"
            )
            if perm_err is not None or skill is None:
                return perm_err or f"Skill `{skill_id}` not found."
            if skill.tags and "builtin" in skill.tags:
                return "Built-in skills cannot be deleted."
            s_name = skill.name
            sid = str(skill.id)
            # Purge stale references from all agents' skill_ids.
            agents_result = await _db.execute(select(Agent))
            for agent in agents_result.scalars().all():
                if agent.skill_ids and sid in agent.skill_ids:
                    agent.skill_ids = [v for v in agent.skill_ids if v != sid]
            await _db.delete(skill)
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder delete_skill failed for skill %s", skill_id
                )
                await _db.rollback()
                return f"Failed to delete skill `{skill_id}`. Please try again."
            return f"Skill **{s_name}** has been deleted."

    # ── Variables ─────────────────────────────────────────────────────

    def _variables_disabled() -> str | None:

        if security_option("disable_custom_variables"):
            return "Custom variables are disabled on this platform."
        return None

    async def _list_variables() -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.variable import Variable

        disabled = _variables_disabled()
        if disabled:
            return disabled
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            q = select(Variable)
            q = q.where(caller_visible_filter(Variable, uid, group_ids))
            q = q.order_by(Variable.name)
            result = await _db.execute(q)
            variables = result.scalars().all()
            if not variables:
                return "No variables found."
            parts: list[str] = [
                f"Found {len(variables)} variable(s) "
                "(reference in prompts/personas with `{{var_id.VARIABLE_ID}}`):\n"
            ]
            for v in variables:
                cm = " [company-managed]" if v.is_company_managed else ""
                secret = " [secret]" if v.is_secret else ""
                shown = "••••••••" if v.is_secret else (v.value or "")
                parts.append(
                    f"• **{v.name}** (id: `{v.id}`){cm}{secret}\n"
                    f"  value: {shown}\n"
                    f"  {v.description or 'No description'}"
                )
            return "\n".join(parts)

    async def _get_variable(variable_id: str) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.variable import Variable

        disabled = _variables_disabled()
        if disabled:
            return disabled
        var_uuid, id_err = _validate_id_arg(variable_id, "variable_id")
        if id_err:
            return id_err
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            q = select(Variable).where(Variable.id == var_uuid)
            q = q.where(caller_visible_filter(Variable, uid, group_ids))
            result = await _db.execute(q)
            v = result.scalar_one_or_none()
            if not v:
                return f"Variable `{variable_id}` not found or you don't have access."
            shown = "••••••••" if v.is_secret else (v.value or "None")
            parts = [
                f"**{v.name}**",
                f"  • ID: `{v.id}`",
                f"  • Value: {shown}",
                f"  • Secret: {'Yes' if v.is_secret else 'No'}",
                f"  • Description: {v.description or 'None'}",
                f"  • Tags: {', '.join(v.tags) if v.tags else 'None'}",
                f"  • Company-managed: {'Yes' if v.is_company_managed else 'No'}",
            ]
            return "\n".join(parts)

    async def _create_variable(
        name: str,
        value: str = "",
        is_secret: bool = False,
        description: str = "",
        tags: str = "",
        is_company_managed: bool = False,
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.core.security import encrypt_value
        from app.models.variable import Variable

        disabled = _variables_disabled()
        if disabled:
            return disabled
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Variable creation is not available for anonymous users."
        clean_name = (name or "").strip()
        if not clean_name:
            return "Variable name is required."
        if problem := _variable_value_problem(value, is_secret):
            return f"Variable not created - {problem}."

        async with _builder_session() as _db:
            caller = await _load_caller_user_for_builder(_db, uid)
            if caller is None:
                return "User not found."
            if is_company_managed and not caller.is_admin:
                return "Only admins can create company-managed variables."
            if is_secret and (problem := await _vault_writer_refusal(uid, {"value": value}, _db)):
                return f"Variable not created - {problem}."
            tag_list = (
                [t.strip() for t in tags.split(",") if t.strip()] if tags else []
            )
            var = Variable(
                user_id=uid,
                name=clean_name,
                is_secret=is_secret,
                description=(description or "").strip() or None,
                tags=tag_list,
                is_company_managed=is_company_managed,
            )
            if is_secret and value:
                var.value_enc = encrypt_value(value)
                var.value = None
            else:
                var.value = value or None
            _db.add(var)
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder create_variable failed for user %s", uid
                )
                await _db.rollback()
                return "Failed to create variable. Please try again."
            return (
                f"Variable **{clean_name}** created successfully!\n"
                f"  • ID: `{var.id}`\n"
                f"  • Reference it as `{{{{var_id.{var.id}}}}}` in prompts, "
                "personas, or tool definitions."
            )

    async def _update_variable(
        variable_id: str,
        name: str = "",
        value: str = "",
        is_secret: str = "",
        description: str = "",
        tags: str = "",
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.core.security import encrypt_value
        from app.models.variable import Variable

        disabled = _variables_disabled()
        if disabled:
            return disabled
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Variable updates are not available for anonymous users."
        var_uuid, id_err = _validate_id_arg(variable_id, "variable_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            var, perm_err = await _load_manageable_entity_for_builder(
                _db, Variable, uid, var_uuid, action="edit", entity_label="variable"
            )
            if perm_err is not None or var is None:
                return perm_err or f"Variable `{variable_id}` not found."

            updated: list[str] = []
            # Free-text fields: a whitespace-only value is "leave unchanged"
            # (consistent with the other update_* builder tools), and the stored
            # value is the stripped form. A real name is always non-empty after
            # the guard, so no separate empty-name rejection is needed here.
            if (name or "").strip():
                var.name = (name or "").strip()
                updated.append("name")
            if (description or "").strip():
                var.description = (description or "").strip()
                updated.append("description")
            if tags:
                var.tags = [t.strip() for t in tags.split(",") if t.strip()]
                updated.append("tags")
            was_secret = var.is_secret
            if is_secret and is_secret.lower() in ("true", "false"):
                var.is_secret = is_secret.lower() == "true"
                updated.append("is_secret")
            # Turning secrecy OFF without a new value would orphan the secret:
            # it lives encrypted in ``value_enc`` and is NOT decrypted into the
            # plaintext ``value`` column here, so the variable would resolve to
            # an empty string at runtime.  Require an explicit new value.
            if (
                was_secret
                and not var.is_secret
                and not value
                and var.value_enc is not None
            ):
                return (
                    "Turning off 'is_secret' requires a new 'value': the existing "
                    "secret is stored encrypted and is not revealed into the "
                    "plaintext column here. Pass the new plaintext as value to set it."
                )
            if value:
                if problem := _variable_value_problem(value, var.is_secret):
                    return f"Variable not updated - {problem}."
                if var.is_secret and (problem := await _vault_writer_refusal(uid, {"value": value}, _db)):
                    return f"Variable not updated - {problem}."
                if var.is_secret:
                    var.value_enc = encrypt_value(value)
                    var.value = None
                else:
                    var.value = value
                    var.value_enc = None
                updated.append("value")
            elif var.is_secret and var.value is not None:
                # No new value, but the variable is secret with a plaintext
                # value still present — either it was just switched to secret in
                # this call, or it's a legacy/inconsistent secret row. Either
                # way, migrate the plaintext into the encrypted column so it
                # isn't left unencrypted at rest (the "secrets encrypted at
                # rest" contract).
                problem = _variable_value_problem(var.value, True) or await _vault_writer_refusal(
                    uid, {"value": var.value}, _db
                )
                if problem:
                    return f"Variable not updated - {problem}."
                var.value_enc = encrypt_value(var.value)
                var.value = None

            if not updated:
                return "No fields to update. Provide at least one field."
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder update_variable failed for variable %s",
                    variable_id,
                )
                await _db.rollback()
                return f"Failed to update variable `{variable_id}`. Please try again."
            return f"Variable **{var.name}** updated: {', '.join(updated)}."

    async def _delete_variable(variable_id: str) -> str:
        from app.core.database import async_session as _builder_session
        from app.models.variable import Variable

        disabled = _variables_disabled()
        if disabled:
            return disabled
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Variable deletion is not available for anonymous users."
        var_uuid, id_err = _validate_id_arg(variable_id, "variable_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            var, perm_err = await _load_manageable_entity_for_builder(
                _db, Variable, uid, var_uuid, action="delete", entity_label="variable"
            )
            if perm_err is not None or var is None:
                return perm_err or f"Variable `{variable_id}` not found."
            v_name = var.name
            await _db.delete(var)
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder delete_variable failed for variable %s",
                    variable_id,
                )
                await _db.rollback()
                return f"Failed to delete variable `{variable_id}`. Please try again."
            return f"Variable **{v_name}** has been deleted."

    # ── MCP Servers ───────────────────────────────────────────────────

    async def _list_mcp_servers() -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.mcp_server import MCPServer

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            q = select(MCPServer)
            q = q.where(caller_visible_filter(MCPServer, uid, group_ids))
            q = q.order_by(MCPServer.name)
            result = await _db.execute(q)
            servers = result.scalars().all()
            if not servers:
                return "No MCP servers found."
            parts: list[str] = [f"Found {len(servers)} MCP server(s):\n"]
            for srv in servers:
                cm = " [company-managed]" if srv.is_company_managed else ""
                enabled = "enabled" if srv.enabled else "disabled"
                tool_count = (
                    len(srv.discovered_tools) if srv.discovered_tools else 0
                )
                parts.append(
                    f"• **{srv.name}** (id: `{srv.id}`, transport: "
                    f"{srv.transport}, {enabled}, {tool_count} tools){cm}"
                )
            return "\n".join(parts)

    async def _get_mcp_server(server_id: str) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.mcp_server import MCPServer

        srv_uuid, id_err = _validate_id_arg(server_id, "server_id")
        if id_err:
            return id_err
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            q = select(MCPServer).where(MCPServer.id == srv_uuid)
            q = q.where(caller_visible_filter(MCPServer, uid, group_ids))
            result = await _db.execute(q)
            srv = result.scalar_one_or_none()
            if not srv:
                return f"MCP server `{server_id}` not found or you don't have access."
            tool_names = (
                ", ".join(t.get("name", "?") for t in srv.discovered_tools)
                if srv.discovered_tools
                else "None (run a test/refresh to discover tools)"
            )
            parts = [
                f"**{srv.name}**",
                f"  • ID: `{srv.id}`",
                f"  • Transport: {srv.transport}",
                f"  • Command: {srv.command or 'None'}",
                # Masked as the MCP API shows it: any user who sees the
                # server reads this, and so does the model.
                f"  • URL: {redact_secret_text(srv.url) or 'None'}",
                f"  • Auth mode: {srv.auth_mode}",
                f"  • Enabled: {'Yes' if srv.enabled else 'No'}",
                f"  • Company-managed: {'Yes' if srv.is_company_managed else 'No'}",
                f"  • Discovered tools: {tool_names}",
            ]
            return "\n".join(parts)

    def _parse_json_object_arg(raw: str, label: str) -> tuple[dict | None, str | None]:
        """Parse a JSON object argument; return ``(dict, None)`` or ``(None, err)``."""
        if not raw:
            return None, None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None, f"Invalid {label}: must be a valid JSON object."
        if not isinstance(parsed, dict):
            return None, f"Invalid {label}: must decode to a JSON object."
        return parsed, None

    async def _stored_extra_config_line(_db: Any, integ: Any) -> str:
        """The extra config as stored after the write, secrets masked: the read-back of an update.

        Masked as the REST responses mask it: the sensitive-name heuristic and
        the secrets a custom bundle's schema declares.
        """
        from app.api.v1.integration_catalog import missing_required_extra_fields
        from app.api.v1.integrations import masked_stored_extra_config

        stored = await masked_stored_extra_config(_db, integ)
        # The validation status (#9898): the catalog's required keys, read on
        # the stored config (masked values count as present).
        missing = missing_required_extra_fields(integ.provider, stored)
        status_line = (
            "valid" if not missing else "invalid: " + ", ".join(f"{key} missing" for key in missing)
        )
        return (
            f"\n  • Extra config now stored (secrets masked): {json.dumps(stored, sort_keys=True, default=str)}"
            f"\n  • Configuration: {status_line}"
        )

    def _platform_base_url_refusal(extra: dict | None) -> str | None:
        """The REST routes' rule (#8386): platform_managed needs a base_url."""
        from app.api.v1.integration_catalog import PLATFORM_MANAGED_NEEDS_BASE_URL, platform_base_url

        return None if platform_base_url(extra) else PLATFORM_MANAGED_NEEDS_BASE_URL

    async def _token_url_refusal(token_url: str) -> str | None:
        """The same token endpoint policy the REST routes apply on save."""
        if not token_url:
            return None
        from app.integrations.token_endpoint import (
            TokenEndpointRefusedError,
            assert_token_endpoint_allowed,
        )

        try:
            await assert_token_endpoint_allowed(token_url)
        except TokenEndpointRefusedError as exc:
            return f"Invalid token_url: {exc}"
        return None

    async def _mcp_url_refusal(url: str, transport: str, *, routed: bool) -> str | None:
        """The MCP server URL policy the REST routes apply on save."""
        from app.tools.mcp_destinations import (
            REMOTE_TRANSPORTS,
            MCPServerURLRefusedError,
            assert_server_url_allowed,
        )

        if transport not in REMOTE_TRANSPORTS:
            return None
        try:
            await assert_server_url_allowed(url, routed=routed)
        except MCPServerURLRefusedError as exc:
            return f"Invalid url: {exc}"
        return None

    async def _create_mcp_server(
        name: str,
        transport: str = "stdio",
        command: str = "",
        args: str = "",
        url: str = "",
        env: str = "",
        headers: str = "",
        is_company_managed: bool = False,
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.core.security import encrypt_value
        from app.models.mcp_server import MCPServer

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "MCP server creation is not available for anonymous users."
        name = (name or "").strip()
        if not name:
            return "MCP server name is required (whitespace-only names are not allowed)."
        valid_transports = ("stdio", "sse", "streamable_http")
        if transport not in valid_transports:
            return f"Invalid transport: must be one of {', '.join(valid_transports)}"
        # Canonicalise the free-text transport fields up front so a
        # whitespace-only value (e.g. ``url=" "``) can't slip past the
        # truthiness checks below and persist a blank, unusable server — the
        # same hardening applied in ``update_mcp_server``. The stripped values
        # are what we store.
        command = (command or "").strip()
        url = (url or "").strip()
        # Fail fast on an unusable transport/field combination so we never
        # persist a broken server that the MCP client runtime would later
        # reject (e.g. "stdio transport requires a 'command'").
        if transport == "stdio" and not command:
            return "Invalid MCP server: the 'stdio' transport requires a 'command'."
        if transport in ("sse", "streamable_http") and not url:
            return (
                f"Invalid MCP server: the '{transport}' transport requires a 'url'."
            )
        if url_error := await _mcp_url_refusal(url, transport, routed=False):
            return url_error

        # ``args`` accepts a JSON array or a comma-separated list.
        arg_list: list[str] = []
        if args:
            try:
                parsed_args = json.loads(args)
                if isinstance(parsed_args, list):
                    arg_list = [str(a) for a in parsed_args]
                else:
                    arg_list = [a.strip() for a in args.split(",") if a.strip()]
            except json.JSONDecodeError:
                arg_list = [a.strip() for a in args.split(",") if a.strip()]

        env_dict, env_err = _parse_json_object_arg(env, "env")
        if env_err:
            return env_err
        headers_dict, headers_err = _parse_json_object_arg(headers, "headers")
        if headers_err:
            return headers_err
        if problem := secret_fields_problem({"env": env_dict, "headers": headers_dict}):
            return f"MCP server not created - {problem}."

        async with _builder_session() as _db:
            caller = await _load_caller_user_for_builder(_db, uid)
            if caller is None:
                return "User not found."
            if is_company_managed and not caller.is_admin:
                return "Only admins can create company-managed MCP servers."
            if problem := await _vault_writer_refusal(uid, {"env": env_dict, "headers": headers_dict}, _db):
                return f"MCP server not created - {problem}."
            srv = MCPServer(
                user_id=uid,
                name=name,
                transport=transport,
                command=command or None,
                args=arg_list or None,
                env_enc=encrypt_value(json.dumps(env_dict)) if env_dict else None,
                url=url or None,
                headers_enc=(
                    encrypt_value(json.dumps(headers_dict)) if headers_dict else None
                ),
                is_company_managed=is_company_managed,
            )
            _db.add(srv)
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder create_mcp_server failed for user %s", uid
                )
                await _db.rollback()
                return "Failed to create MCP server. Please try again."
            return (
                f"MCP server **{name}** created successfully!\n"
                f"  • ID: `{srv.id}`\n"
                "Tools are discovered when the server is first used or tested. "
                "Attach it to an agent with `update_agent` (mcp_server_ids)."
            )

    async def _update_mcp_server(
        server_id: str,
        name: str = "",
        transport: str = "",
        command: str = "",
        args: str = "",
        url: str = "",
        env: str = "",
        headers: str = "",
        auth_mode: str = "",
        client_id: str = "",
        client_secret: str = "",
        scopes: str = "",
        authorize_url: str = "",
        token_url: str = "",
        tags: str = "",
        hide_from_chat: str = "",
        enabled: str = "",
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.core.security import encrypt_value
        from app.models.mcp_server import MCPServer

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "MCP server updates are not available for anonymous users."
        srv_uuid, id_err = _validate_id_arg(server_id, "server_id")
        if id_err:
            return id_err

        # Tool-call arguments are forwarded without type coercion, so a provider
        # can send JSON ``null`` for any string field. Canonicalise the free-text
        # transport fields to their stripped form up front so a whitespace-only
        # value is treated as "leave unchanged" and a ``null`` never reaches a
        # bare ``.strip()`` (which would raise ``AttributeError`` and surface as
        # an opaque tool-execution error). Secrets (``client_id`` /
        # ``client_secret``) are intentionally excluded — they must be stored
        # verbatim.
        transport = (transport or "").strip()
        command = (command or "").strip()
        url = (url or "").strip()
        args = (args or "").strip()
        auth_mode = (auth_mode or "").strip()
        scopes = (scopes or "").strip()
        authorize_url = (authorize_url or "").strip()
        token_url = (token_url or "").strip()
        if token_url_error := await _token_url_refusal(token_url):
            return token_url_error

        valid_transports = ("stdio", "sse", "streamable_http")
        if transport and transport not in valid_transports:
            return f"Invalid transport: must be one of {', '.join(valid_transports)}"
        valid_auth_modes = ("headers", "oauth")
        if auth_mode and auth_mode not in valid_auth_modes:
            return f"Invalid auth_mode: must be one of {', '.join(valid_auth_modes)}"

        env_dict, env_err = _parse_json_object_arg(env, "env")
        if env_err:
            return env_err
        headers_dict, headers_err = _parse_json_object_arg(headers, "headers")
        if headers_err:
            return headers_err
        if problem := secret_fields_problem(
            {"env": env_dict, "headers": headers_dict},
            {"client_id": client_id, "client_secret": client_secret},
        ):
            return f"MCP server not updated - {problem}."

        async with _builder_session() as _db:
            # Locked: the stored-reference check below and the write hold the row together.
            srv, perm_err = await _load_manageable_entity_for_builder(
                _db,
                MCPServer,
                uid,
                srv_uuid,
                action="edit",
                entity_label="MCP server",
                lock=True,
            )
            if perm_err is not None or srv is None:
                return perm_err or f"MCP server `{server_id}` not found."
            if problem := await _vault_writer_refusal(uid, {"env": env_dict, "headers": headers_dict}, _db):
                return f"MCP server not updated - {problem}."
            new_args = _mcp_args(args) if args else None
            if new_args is not None:
                # The same rule as ``PUT /mcp-servers/{id}``: a masked argument
                # copied from the UI keeps the value it stands for. Restored
                # before the re-point check, which compares it to the stored one.
                try:
                    new_args = restore_masked_args(new_args, srv.args)
                except MaskedArgumentError:
                    return (
                        "An argument is still masked and cannot be matched to "
                        "exactly one stored value. Ask the user for the full "
                        "value of each masked argument."
                    )
            # A masked URL pasted back as ``get_mcp_server`` / the MCP API show
            # it keeps the stored one (the same rule as ``PUT
            # /mcp-servers/{id}``), restored before the re-point check so a URL
            # sent back as shown is no new destination; the token endpoint
            # policy judges the URL that is saved too, not only its mask.
            restored_urls: dict[str, str] = {}
            for field, value in (
                ("url", url),
                ("authorize_url", authorize_url),
                ("token_url", token_url),
            ):
                if not value:
                    continue
                label = MCP_SERVER_URL_LABELS[field]
                try:
                    restored_urls[field] = restore_masked_text(value, getattr(srv, field))
                except MaskedArgumentError:
                    return (
                        f"The {label} is still masked and does not match the "
                        f"stored one. Ask the user for the full {label}."
                    )
                if field == "token_url" and restored_urls[field] != value and (
                    token_url_error := await _token_url_refusal(restored_urls[field])
                ):
                    return token_url_error
            url = restored_urls.get("url", url)
            authorize_url = restored_urls.get("authorize_url", authorize_url)
            token_url = restored_urls.get("token_url", token_url)
            # Judged as the server will be once saved, before any write, on the
            # URL restored above, and only when where it connects changes (as
            # the REST update does).
            if url or transport:
                new_url, new_transport = url or srv.url or "", transport or srv.transport
                if (new_url, new_transport) != ((srv.url or "").strip(), srv.transport) and (
                    url_error := await _mcp_url_refusal(
                        new_url,
                        new_transport,
                        routed=getattr(srv, "relay_group_id", None) is not None,
                    )
                ):
                    return url_error
            # Blank strings mean "leave unchanged" here.
            destination = {
                "transport": transport,
                "command": command,
                "url": url,
                "auth_mode": auth_mode,
                "authorize_url": authorize_url,
                "token_url": token_url,
            }
            changes: dict[str, Any] = {field: value for field, value in destination.items() if value}
            if new_args is not None:
                changes["args"] = new_args
            repoints = any(getattr(srv, field) != value for field, value in changes.items())
            replaced = {column for column, value in (("env_enc", env_dict), ("headers_enc", headers_dict)) if value is not None}
            if client_secret:
                replaced.add("client_secret_enc")
            if problem := await _retained_reference_refusal(srv, uid, _db, repoints=repoints, replaced=replaced):
                return f"MCP server not updated - {problem}."

            updated: list[str] = []
            if (name or "").strip():
                srv.name = (name or "").strip()
                updated.append("name")
            if transport:
                srv.transport = transport
                updated.append("transport")
            # Free-text fields are canonicalised (stripped) up front so a
            # whitespace-only value is treated as "leave unchanged" (it can't
            # persist a blank/unusable command, url, scopes, etc.) — mirroring
            # the ``llm_model`` hardening in ``update_agent``.
            if command:
                srv.command = command
                updated.append("command")
            if new_args is not None:
                srv.args = new_args
                updated.append("args")
            # Restored above, before the re-point check.
            for field, value in (
                ("url", url),
                ("authorize_url", authorize_url),
                ("token_url", token_url),
            ):
                if value:
                    setattr(srv, field, value)
                    updated.append(field)
            if env_dict is not None:
                srv.env_enc = encrypt_value(json.dumps(env_dict))
                updated.append("env")
            if headers_dict is not None:
                srv.headers_enc = encrypt_value(json.dumps(headers_dict))
                updated.append("headers")
            if auth_mode:
                srv.auth_mode = auth_mode
                # Switching back to header auth invalidates any existing OAuth
                # session, mirroring the REST ``update_mcp_server`` reset.
                if srv.auth_mode == "headers":
                    srv.oauth_status = "disconnected"
                    srv.access_token_enc = None
                    srv.refresh_token_enc = None
                    srv.token_expires_at = None
                    srv.token_issued_at = None
                updated.append("auth_mode")
            # OAuth client credentials are secrets: store encrypted, never echo,
            # and never strip (trimming could corrupt the credential).
            if client_id:
                srv.client_id_enc = encrypt_value(client_id)
                updated.append("client_id")
            if client_secret:
                srv.client_secret_enc = encrypt_value(client_secret)
                updated.append("client_secret")
            if scopes:
                srv.scopes = scopes
                updated.append("scopes")
            if tags:
                srv.tags = [t.strip() for t in tags.split(",") if t.strip()]
                updated.append("tags")
            if hide_from_chat and hide_from_chat.lower() in ("true", "false"):
                srv.hide_from_chat = hide_from_chat.lower() == "true"
                updated.append("hide_from_chat")
            if enabled and enabled.lower() in ("true", "false"):
                srv.enabled = enabled.lower() == "true"
                updated.append("enabled")

            if not updated:
                return "No fields to update. Provide at least one field."

            # When transport/command/url change, re-validate the resulting
            # config against the same constraints as create_mcp_server so a
            # transport switch can't leave a server the MCP runtime would
            # reject (e.g. switching to 'sse'/'streamable_http' with no url, or
            # to 'stdio' with no command). The early return happens before the
            # commit, so a rejected switch persists nothing.
            if {"transport", "command", "url"} & set(updated):
                if srv.transport == "stdio" and not srv.command:
                    return (
                        "Invalid MCP server: the 'stdio' transport requires a "
                        "'command'. Provide 'command' in the same update."
                    )
                if srv.transport in ("sse", "streamable_http") and not srv.url:
                    return (
                        f"Invalid MCP server: the '{srv.transport}' transport "
                        "requires a 'url'. Provide 'url' in the same update."
                    )
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder update_mcp_server failed for server %s", server_id
                )
                await _db.rollback()
                return f"Failed to update MCP server `{server_id}`. Please try again."
            # Never echo credentials — only report which fields changed.
            return f"MCP server **{srv.name}** updated: {', '.join(updated)}."

    async def _delete_mcp_server(server_id: str) -> str:
        from sqlalchemy import select

        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.mcp_server import MCPServer

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "MCP server deletion is not available for anonymous users."
        srv_uuid, id_err = _validate_id_arg(server_id, "server_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            srv, perm_err = await _load_manageable_entity_for_builder(
                _db,
                MCPServer,
                uid,
                srv_uuid,
                action="delete",
                entity_label="MCP server",
            )
            if perm_err is not None or srv is None:
                return perm_err or f"MCP server `{server_id}` not found."
            s_name = srv.name
            sid = str(srv.id)
            # Purge stale references from all agents' mcp_server_ids.
            agents_result = await _db.execute(select(Agent))
            for agent in agents_result.scalars().all():
                if agent.mcp_server_ids and sid in agent.mcp_server_ids:
                    agent.mcp_server_ids = [
                        v for v in agent.mcp_server_ids if v != sid
                    ]
            await _db.delete(srv)
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder delete_mcp_server failed for server %s", server_id
                )
                await _db.rollback()
                return f"Failed to delete MCP server `{server_id}`. Please try again."
            return f"MCP server **{s_name}** has been deleted."

    # ── Custom Tools (create / update / delete) ───────────────────────

    def _custom_tools_disabled() -> str | None:

        if security_option("disable_custom_tools"):
            return "Custom tools are disabled on this platform."
        return None

    async def _create_custom_tool(
        name: str,
        description: str = "",
        js_function: str = "",
        input_schema: str = "",
        language: str = "javascript",
        tags: str = "",
        is_company_managed: bool = False,
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.models.custom_tool import CustomTool

        disabled = _custom_tools_disabled()
        if disabled:
            return disabled
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Custom tool creation is not available for anonymous users."
        name = (name or "").strip()
        if not name:
            return (
                "Custom tool name is required (whitespace-only names are not "
                "allowed)."
            )
        if language not in ("javascript", "python"):
            return "Invalid language: must be 'javascript' or 'python'."

        schema_obj, schema_err = _parse_json_object_arg(input_schema, "input_schema")
        if schema_err:
            return schema_err

        async with _builder_session() as _db:
            caller = await _load_caller_user_for_builder(_db, uid)
            if caller is None:
                return "User not found."
            if is_company_managed and not caller.is_admin:
                return "Only admins can create company-managed tools."
            tag_list = (
                [t.strip() for t in tags.split(",") if t.strip()] if tags else []
            )
            tool = CustomTool(
                user_id=uid,
                name=name,
                description=(description or "").strip(),
                input_schema=schema_obj,
                js_function=js_function or None,
                language=language,
                tags=tag_list,
                is_company_managed=is_company_managed,
            )
            _db.add(tool)
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder create_custom_tool failed for user %s", uid
                )
                await _db.rollback()
                return "Failed to create custom tool. Please try again."
            return (
                f"Custom tool **{name}** created successfully!\n"
                f"  • ID: `{tool.id}`\n"
                "Attach it to an agent with `update_agent` (custom_tool_ids). "
                "Test it via the Custom Tools UI before relying on it."
            )

    async def _update_custom_tool(
        tool_id: str,
        name: str = "",
        description: str = "",
        js_function: str = "",
        input_schema: str = "",
        language: str = "",
        tags: str = "",
        enabled: str = "",
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.models.custom_tool import CustomTool

        disabled = _custom_tools_disabled()
        if disabled:
            return disabled
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Custom tool updates are not available for anonymous users."
        tool_uuid, id_err = _validate_id_arg(tool_id, "tool_id")
        if id_err:
            return id_err
        if language and language not in ("javascript", "python"):
            return "Invalid language: must be 'javascript' or 'python'."

        schema_obj, schema_err = _parse_json_object_arg(input_schema, "input_schema")
        if schema_err:
            return schema_err

        async with _builder_session() as _db:
            tool, perm_err = await _load_manageable_entity_for_builder(
                _db,
                CustomTool,
                uid,
                tool_uuid,
                action="edit",
                entity_label="custom tool",
            )
            if perm_err is not None or tool is None:
                return perm_err or f"Custom tool `{tool_id}` not found."
            if origin_err := await package_origin_custom_tool_error(_db, tool.id):
                return f"{origin_err}."
            # Only ``enabled`` stays writable on a tool synced from a repository.
            content_change = bool(
                (name or "").strip()
                or (description or "").strip()
                or js_function
                or schema_obj is not None
                or language
                or tags
            )
            if content_change and (
                synced_err := await source_managed_custom_tool_error(_db, tool)
            ):
                return f"{synced_err}."

            updated: list[str] = []
            # Free-text fields: whitespace-only = "leave unchanged", stored
            # value is the stripped form.
            if (name or "").strip():
                tool.name = (name or "").strip()
                updated.append("name")
            if (description or "").strip():
                tool.description = (description or "").strip()
                updated.append("description")
            if js_function:
                tool.js_function = js_function
                updated.append("js_function")
            if schema_obj is not None:
                tool.input_schema = schema_obj
                updated.append("input_schema")
            if language:
                tool.language = language
                updated.append("language")
            if tags:
                tool.tags = [t.strip() for t in tags.split(",") if t.strip()]
                updated.append("tags")
            if enabled and enabled.lower() in ("true", "false"):
                tool.enabled = enabled.lower() == "true"
                updated.append("enabled")

            if not updated:
                return "No fields to update. Provide at least one field."
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder update_custom_tool failed for tool %s", tool_id
                )
                await _db.rollback()
                return f"Failed to update custom tool `{tool_id}`. Please try again."
            return f"Custom tool **{tool.name}** updated: {', '.join(updated)}."

    async def _delete_custom_tool(tool_id: str) -> str:
        from sqlalchemy import select

        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.custom_tool import CustomTool

        disabled = _custom_tools_disabled()
        if disabled:
            return disabled
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Custom tool deletion is not available for anonymous users."
        tool_uuid, id_err = _validate_id_arg(tool_id, "tool_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            tool, perm_err = await _load_manageable_entity_for_builder(
                _db,
                CustomTool,
                uid,
                tool_uuid,
                action="delete",
                entity_label="custom tool",
            )
            if perm_err is not None or tool is None:
                return perm_err or f"Custom tool `{tool_id}` not found."
            if origin_err := await package_origin_custom_tool_error(
                _db, tool.id, action="deleted"
            ):
                return f"{origin_err}."
            if synced_err := await source_managed_custom_tool_error(
                _db, tool, action="deleted"
            ):
                return f"{synced_err}."
            t_name = tool.name
            tid = str(tool.id)
            agents_result = await _db.execute(select(Agent))
            for agent in agents_result.scalars().all():
                if agent.custom_tool_ids and tid in agent.custom_tool_ids:
                    agent.custom_tool_ids = [
                        v for v in agent.custom_tool_ids if v != tid
                    ]
            await _db.delete(tool)
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder delete_custom_tool failed for tool %s", tool_id
                )
                await _db.rollback()
                return f"Failed to delete custom tool `{tool_id}`. Please try again."
            return f"Custom tool **{t_name}** has been deleted."

    # ── Knowledge Bases (get / update / delete + documents) ───────────

    async def _get_knowledge_base(knowledge_base_id: str) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.knowledge_base import KnowledgeBase

        kb_uuid, id_err = _validate_id_arg(knowledge_base_id, "knowledge_base_id")
        if id_err:
            return id_err
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            q = select(KnowledgeBase).where(KnowledgeBase.id == kb_uuid)
            q = q.where(caller_visible_filter(KnowledgeBase, uid, group_ids))
            result = await _db.execute(q)
            kb = result.scalar_one_or_none()
            if not kb:
                return (
                    f"Knowledge base `{knowledge_base_id}` not found or "
                    "you don't have access."
                )
            parts = [
                f"**{kb.name}**",
                f"  • ID: `{kb.id}`",
                f"  • Description: {kb.description or 'None'}",
                f"  • Documents: {kb.document_count}",
                f"  • Chunks: {kb.chunk_count}",
                f"  • Tags: {', '.join(kb.tags) if kb.tags else 'None'}",
                f"  • Company-managed: {'Yes' if kb.is_company_managed else 'No'}",
                f"  • Built-in: {'Yes' if kb.is_builtin else 'No'}",
            ]
            return "\n".join(parts)

    async def _update_knowledge_base(
        knowledge_base_id: str,
        name: str = "",
        description: str = "",
        tags: str = "",
        is_company_managed: str = "",
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.models.knowledge_base import KnowledgeBase

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Knowledge base updates are not available for anonymous users."
        kb_uuid, id_err = _validate_id_arg(knowledge_base_id, "knowledge_base_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            kb, perm_err = await _load_manageable_entity_for_builder(
                _db,
                KnowledgeBase,
                uid,
                kb_uuid,
                action="edit",
                entity_label="knowledge base",
            )
            if perm_err is not None or kb is None:
                return perm_err or f"Knowledge base `{knowledge_base_id}` not found."
            if kb.is_builtin:
                return "Built-in knowledge bases cannot be modified."
            if origin_err := await package_origin_knowledge_base_error(_db, kb.id):
                return f"{origin_err}."
            if workspace_err := await workspace_managed_knowledge_base_error(_db, kb):
                return workspace_err

            updated: list[str] = []
            # Free-text fields: whitespace-only = "leave unchanged", stored
            # value is the stripped form.
            if (name or "").strip():
                kb.name = (name or "").strip()
                updated.append("name")
            if (description or "").strip():
                kb.description = (description or "").strip()
                updated.append("description")
            if tags:
                kb.tags = [t.strip() for t in tags.split(",") if t.strip()]
                updated.append("tags")

            # Company-managed (platform-wide visibility) toggle. Parsed like a
            # boolean string; a whitespace-only / unset value leaves it
            # unchanged. Editing is already gated above, but flipping
            # platform-wide visibility is a stricter, distinct capability (a
            # non-admin owner can edit their own private KB yet must never be
            # able to publish it company-wide), so it carries its own admin gate
            # — mirroring the REST ``PUT /knowledge-bases/{id}`` contract via the
            # shared ``handle_company_managed_toggle`` helper (which also strips
            # the KB from other users' agents on the company -> private flip and
            # clears group sharing on the private -> company flip).
            if (is_company_managed or "").strip().lower() in ("true", "false"):
                want_cm = (is_company_managed or "").strip().lower() == "true"
                if want_cm != bool(kb.is_company_managed):
                    caller = await _load_caller_user_for_builder(_db, uid)
                    if caller is None or not caller.is_admin:
                        return (
                            "Only admins can change the company-managed "
                            "(platform-wide visibility) status of a knowledge "
                            "base."
                        )
                    from fastapi import HTTPException

                    from app.api.v1.helpers import handle_company_managed_toggle

                    try:
                        await handle_company_managed_toggle(
                            _db,
                            caller,
                            kb,
                            want_cm,
                            agent_field="knowledge_base_ids",
                        )
                    except HTTPException as exc:
                        await _db.rollback()
                        return str(exc.detail)
                    updated.append("company-managed")

            if not updated:
                return "No fields to update. Provide at least one field."
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder update_knowledge_base failed for kb %s",
                    knowledge_base_id,
                )
                await _db.rollback()
                return (
                    f"Failed to update knowledge base `{knowledge_base_id}`. "
                    "Please try again."
                )
            return f"Knowledge base **{kb.name}** updated: {', '.join(updated)}."

    async def _delete_knowledge_base(knowledge_base_id: str) -> str:
        from sqlalchemy import delete as sa_delete
        from sqlalchemy import select

        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.assignment import Assignment
        from app.models.embedding import Embedding
        from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Knowledge base deletion is not available for anonymous users."
        kb_uuid, id_err = _validate_id_arg(knowledge_base_id, "knowledge_base_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            kb, perm_err = await _load_manageable_entity_for_builder(
                _db,
                KnowledgeBase,
                uid,
                kb_uuid,
                action="delete",
                entity_label="knowledge base",
            )
            if perm_err is not None or kb is None:
                return perm_err or f"Knowledge base `{knowledge_base_id}` not found."
            # Caller rights were checked by the loader; the KB's own locks
            # (package-installed, built-in, read-only) come from the shared gate.
            if write_err := await writable_knowledge_base_error(
                _db, kb, content_action="delete it", participle="deleted"
            ):
                return _as_sentence(write_err)

            kb_name = kb.name
            kid = str(kb.id)
            # Block deletion of a system-managed KB still bound to an agent.
            if getattr(kb, "is_system_managed", False):
                agents_result = await _db.execute(select(Agent))
                for agent in agents_result.scalars().all():
                    if agent.knowledge_base_ids and kid in agent.knowledge_base_ids:
                        return (
                            f"Knowledge base '{kb_name}' is system-managed and bound "
                            f"to agent '{agent.name}'. Unbind it first."
                        )

            # Purge references from agents + assignments, then cascade-delete docs.
            agents_result = await _db.execute(select(Agent))
            for agent in agents_result.scalars().all():
                if agent.knowledge_base_ids and kid in agent.knowledge_base_ids:
                    agent.knowledge_base_ids = [
                        v for v in agent.knowledge_base_ids if v != kid
                    ]
            assignments_result = await _db.execute(select(Assignment))
            for assignment in assignments_result.scalars().all():
                if (
                    assignment.read_knowledge_base_ids
                    and kid in assignment.read_knowledge_base_ids
                ):
                    assignment.read_knowledge_base_ids = [
                        v for v in assignment.read_knowledge_base_ids if v != kid
                    ]
                if (
                    assignment.target_knowledge_base_id
                    and str(assignment.target_knowledge_base_id) == kid
                ):
                    assignment.target_knowledge_base_id = None

            doc_ids = [
                str(doc_id)
                for doc_id in (
                    await _db.execute(
                        select(KnowledgeDocument.id).where(
                            KnowledgeDocument.knowledge_base_id == kb.id
                        )
                    )
                )
                .scalars()
                .all()
            ]
            await _db.execute(
                sa_delete(Embedding).where(Embedding.knowledge_base_id == kb.id)
            )
            await _db.execute(
                sa_delete(KnowledgeDocument).where(
                    KnowledgeDocument.knowledge_base_id == kb.id
                )
            )
            from app.services.kb_helpers import cancel_document_jobs_after_commit

            cancel_document_jobs_after_commit(_db, doc_ids)
            await _db.delete(kb)
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder delete_knowledge_base failed for kb %s",
                    knowledge_base_id,
                )
                await _db.rollback()
                return (
                    f"Failed to delete knowledge base `{knowledge_base_id}`. "
                    "Please try again."
                )
            return f"Knowledge base **{kb_name}** and its documents have been deleted."

    async def _list_kb_documents(knowledge_base_id: str) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

        kb_uuid, id_err = _validate_id_arg(knowledge_base_id, "knowledge_base_id")
        if id_err:
            return id_err
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            kb_q = select(KnowledgeBase).where(KnowledgeBase.id == kb_uuid)
            kb_q = kb_q.where(caller_visible_filter(KnowledgeBase, uid, group_ids))
            kb = (await _db.execute(kb_q)).scalar_one_or_none()
            if not kb:
                return (
                    f"Knowledge base `{knowledge_base_id}` not found or "
                    "you don't have access."
                )
            docs_result = await _db.execute(
                select(KnowledgeDocument)
                .where(KnowledgeDocument.knowledge_base_id == kb.id)
                .order_by(KnowledgeDocument.created_at.desc())
                .limit(100)
            )
            docs = docs_result.scalars().all()
            if not docs:
                return f"Knowledge base **{kb.name}** has no documents."
            parts: list[str] = [
                f"Documents in **{kb.name}** ({len(docs)} shown):\n"
            ]
            for d in docs:
                parts.append(
                    f"• **{d.filename}** (id: `{d.id}`, type: {d.source_type}, "
                    f"status: {d.status})"
                )
            return "\n".join(parts)

    async def _get_kb_document(knowledge_base_id: str, document_id: str) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

        kb_uuid, id_err = _validate_id_arg(knowledge_base_id, "knowledge_base_id")
        if id_err:
            return id_err
        doc_uuid, doc_err = _validate_id_arg(document_id, "document_id")
        if doc_err:
            return doc_err
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            # Read-access visibility mirrors ``list_kb_documents`` (own +
            # group-shared + company-managed). If you can list the document
            # you can get it; private KBs owned by other users stay hidden.
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            kb_q = select(KnowledgeBase).where(KnowledgeBase.id == kb_uuid)
            kb_q = kb_q.where(caller_visible_filter(KnowledgeBase, uid, group_ids))
            kb = (await _db.execute(kb_q)).scalar_one_or_none()
            if not kb:
                return (
                    f"Knowledge base `{knowledge_base_id}` not found or "
                    "you don't have access."
                )
            doc = (
                await _db.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.id == doc_uuid,
                        KnowledgeDocument.knowledge_base_id == kb.id,
                    )
                )
            ).scalar_one_or_none()
            if not doc:
                return (
                    f"Document `{document_id}` not found in knowledge base "
                    f"**{kb.name}**."
                )

            # Exact, deterministic fetch by id — NOT semantic ranking. The raw
            # text is stored on the document row at insert time, so it is
            # readable here even while embedding is still pending/processing
            # (the write-then-read case ``search_knowledge`` cannot serve).
            parts: list[str] = [
                f"Document **{doc.filename}** (id: `{doc.id}`)",
                f"  • Knowledge base: {kb.name} (`{kb.id}`)",
                f"  • Source type: {doc.source_type}",
                f"  • Processing status: {doc.status}",
                f"  • Chunks: {doc.chunk_count or 0}",
            ]
            if doc.source_url:
                parts.append(f"  • Source URL: {doc.source_url}")
            if doc.created_at:
                parts.append(f"  • Created: {doc.created_at.isoformat()}")
            if doc.updated_at:
                parts.append(f"  • Updated: {doc.updated_at.isoformat()}")
            if doc.status != "ready":
                parts.append(
                    "  • Note: this document is not fully embedded yet — its "
                    "full content is shown below, but it is not yet searchable "
                    "via search_knowledge."
                )
            if doc.status == "error" and doc.error_message:
                from app.services.kb_document_errors import public_error_message

                parts.append(f"  • Error: {public_error_message(doc.error_message)}")

            # Surface the content in FULL (never self-truncated), reassembled
            # from the stored original document — same rationale as get_skill /
            # get_assignment; the agent loop caps oversize results centrally.
            # ``is not None`` (not a truthiness check): ``content`` is nullable,
            # so an intentionally-empty document (``content == ""``) is a real,
            # distinct state from "no stored text" (``None``) and must round-trip
            # as empty content from this exact-get tool, not the NULL fallback.
            parts.append("")
            if doc.content is not None:
                parts.append("Content (full):")
                parts.append(doc.content)
            else:
                parts.append(
                    "Content (full): (no stored text content for this document)"
                )
            return "\n".join(parts)

    async def _delete_kb_document(knowledge_base_id: str, document_id: str) -> str:
        from sqlalchemy import select

        from app.core.database import async_session as _builder_session
        from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Document deletion is not available for anonymous users."
        kb_uuid, id_err = _validate_id_arg(knowledge_base_id, "knowledge_base_id")
        if id_err:
            return id_err
        doc_uuid, doc_err = _validate_id_arg(document_id, "document_id")
        if doc_err:
            return doc_err

        async with _builder_session() as _db:
            kb, perm_err = await _load_manageable_entity_for_builder(
                _db,
                KnowledgeBase,
                uid,
                kb_uuid,
                action="delete documents from",
                entity_label="knowledge base",
            )
            if perm_err is not None or kb is None:
                return perm_err or f"Knowledge base `{knowledge_base_id}` not found."
            # Caller rights were checked by the loader; the KB's own locks
            # (package-installed, built-in, read-only) come from the shared gate.
            if write_err := await writable_knowledge_base_error(
                _db, kb, content_action="delete documents from it"
            ):
                return _as_sentence(write_err)

            doc = (
                await _db.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.id == doc_uuid,
                        KnowledgeDocument.knowledge_base_id == kb.id,
                    )
                )
            ).scalar_one_or_none()
            if not doc:
                return f"Document `{document_id}` not found in this knowledge base."

            from app.services.kb_helpers import (
                bump_kb_revision,
                cancel_document_jobs_after_commit,
            )

            doc_name = doc.filename
            doc_chunks = doc.chunk_count or 0
            cancel_document_jobs_after_commit(_db, [doc.id])
            # Also delete the document's embedding chunks: they are keyed by
            # ``source_id == str(doc.id)`` (a plain string, not a FK — only
            # ``knowledge_base_id`` cascades), so they are NOT removed when the
            # document row is deleted and would keep surfacing in RAG / hybrid
            # search.  Keep the KB's denormalised ``chunk_count`` in sync.
            from sqlalchemy import delete as sa_delete

            from app.models.embedding import Embedding

            await _db.execute(
                sa_delete(Embedding).where(
                    Embedding.knowledge_base_id == kb.id,
                    Embedding.source_type == "document",
                    Embedding.source_id == str(doc.id),
                )
            )
            await _db.delete(doc)
            kb.document_count = max(0, (kb.document_count or 0) - 1)
            await bump_kb_revision(_db, kb.id)
            # Decrement whenever the document had chunks. ``chunk_count`` is
            # expected to be non-null but may be NULL/desynced on legacy or
            # inconsistent rows (hence the ``(kb.chunk_count or 0)`` guard
            # below); a falsy-``kb.chunk_count`` guard here would skip a
            # legitimate, ``max(0, …)``-floored decrement and leave the counter
            # wrong.
            if doc_chunks:
                kb.chunk_count = max(0, (kb.chunk_count or 0) - doc_chunks)
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder delete_kb_document failed for doc %s", document_id
                )
                await _db.rollback()
                return f"Failed to delete document `{document_id}`. Please try again."
            return f"Document **{doc_name}** deleted from **{kb.name}**."

    # ── Integrations (catalog / get / create / update / delete) ───────

    async def _list_platform_events(search: str = "") -> str:
        """The platform events a ``platform_event`` trigger subscribes to."""
        from app.services.platform_events import catalog

        needle = (search or "").strip().lower()
        rows = [
            e for e in catalog() if not needle or needle in e["event"] or needle in e["description"].lower()
        ]
        if not rows:
            return "No platform event matches."
        lines = [
            "Platform events (trigger_type 'platform_event', trigger_config "
            '{"event": one event name below or a list of them, '
            '"filter": {"status": "done"} (the filter keys below, dotted paths; a list value means any of; '
            'a value on a list-valued key such as fleet_impact.targets matches when it is one of its elements; '
            'a key also takes an operator object: eq, ne, in, not_in, starts_with, ends_with, contains, '
            'matches (a whole-value regular expression), exists, e.g. {"correlation_key": {"starts_with": "mail:"}}, '
            'and filters combine with all / any (lists of filters) and not (one filter)), '
            '"scope": "own", "fleet" or "all", '
            '"once": true or false, "expires_at": "2026-11-02T09:00:00Z" (optional deadline)}; '
            "'own' (default) delivers only events on objects the assignment's agent created, "
            "owns, holds or is the parent of; 'fleet' delivers the events of every home member of "
            "the agent's fleet (membership read at delivery; refused for an agent in no fleet); "
            "'all' needs an administrator's assignment; "
            "'once' true makes it a one-off watch: the first matching event starts one run, "
            "then the subscription ends (save the trigger again to re-arm it); "
            "'expires_at' (ISO 8601 with its offset) ends the subscription at that time, "
            "so an event raised after it starts nothing:"
        ]
        for e in rows:
            lines.append(f"- {e['event']}: {e['description']} Filter keys: {', '.join(e['filter_keys'])}")
        return "\n".join(lines)

    async def _watch_platform_event(
        agent_id: str = "",
        task: str = "",
        event: Any = None,
        expires_at: str = "",
        filter: Any = None,
        label: str = "",
    ) -> str:
        """Set a one-off watch that wakes a background task of *agent_id*."""
        from app.core.database import async_session as _builder_session
        from app.services.platform_event_task_watches import TaskWatchError, create_watch

        uid = safe_uuid(user_id)
        if uid is None:
            return "Watching a platform event is not available for anonymous users."
        agent_uuid, id_err = _validate_id_arg(agent_id, "agent_id")
        if id_err:
            return id_err
        if isinstance(filter, str) and filter.strip():
            try:
                filter = json.loads(filter)
            except ValueError:
                return "filter must be a JSON object, for example {\"correlation_key\": \"dev:x\"}."
        async with _builder_session() as _db:
            agent, perm_err = await _load_writable_agent_for_builder(_db, uid, agent_uuid, action="run")
            if perm_err is not None or agent is None:
                return perm_err or f"Agent `{agent_id}` not found or you don't have access."
            if agent.enabled is False:
                return f"Agent **{agent.name}** is disabled. Enable it first."
            try:
                watch = await create_watch(
                    _db,
                    user_id=uid,
                    agent_id=agent_uuid,
                    task=task,
                    events=event,
                    filter=filter,
                    expires_at=expires_at,
                    label=label,
                )
            except TaskWatchError as exc:
                return f"Watch not set: {exc}"
        return (
            f"Watch `{watch.id}` armed on {', '.join(watch.events)} until {watch.expires_at.isoformat()}: "
            f"the first matching event starts one background task of **{agent.name}**, then the watch ends. "
            "cancel_platform_event_watch removes it; list_platform_event_watches shows its state."
        )

    async def _list_platform_event_watches(include_ended: bool = False) -> str:
        """The caller's task watches."""
        from app.core.database import async_session as _builder_session
        from app.services.platform_event_task_watches import list_watches, state

        uid = safe_uuid(user_id)
        if uid is None:
            return "No watch: anonymous user."
        async with _builder_session() as _db:
            rows = await list_watches(_db, user_id=uid, include_ended=include_ended is True)
        if not rows:
            return "No platform event watch." if include_ended is True else "No armed platform event watch."
        lines = []
        for w in rows:
            line = (
                f"- `{w.id}` [{state(w)}] {', '.join(w.events or [])} for agent `{w.agent_id}`, "
                f"until {w.expires_at.isoformat()}"
            )
            if w.label:
                line += f" ({w.label})"
            if w.task_id:
                line += f", started task `{w.task_id}`"
            lines.append(line)
        return "\n".join(lines)

    async def _cancel_platform_event_watch(watch_id: str = "") -> str:
        """Cancel one of the caller's armed task watches."""
        from app.core.database import async_session as _builder_session
        from app.services.platform_event_task_watches import cancel_watch, state

        uid = safe_uuid(user_id)
        if uid is None:
            return "No watch: anonymous user."
        watch_uuid, id_err = _validate_id_arg(watch_id, "watch_id")
        if id_err:
            return id_err
        async with _builder_session() as _db:
            watch = await cancel_watch(_db, user_id=uid, watch_id=watch_uuid)
        if watch is None:
            return f"Watch `{watch_id}` not found."
        return f"Watch `{watch.id}` is {state(watch)}."

    async def _list_integration_catalog(search: str = "") -> str:
        from app.api.v1.integration_catalog import PROVIDER_CATALOG

        term = (search or "").strip().lower()
        parts: list[str] = ["Available integration providers:\n"]
        for key, cfg in sorted(PROVIDER_CATALOG.items()):
            name = cfg.get("name", key)
            if term and term not in key.lower() and term not in name.lower():
                continue
            fields = cfg.get("fields") or []
            # ``extra_fields`` is a ``dict[str, str]`` mapping field key -> label
            # (see ``schemas/integration.py``), so its keys are the field names.
            extra = list((cfg.get("extra_fields") or {}).keys())
            field_str = ", ".join([*fields, *extra]) or "none"
            parts.append(
                f"• `{key}` — {name} (auth: {cfg.get('auth_type', 'n/a')})\n"
                f"  {cfg.get('description', '')}\n"
                f"  fields: {field_str}"
            )
            # The integration_event triggers the provider serves, with the
            # filter keys each takes (trigger_config.filter).
            for ev in cfg.get("available_events") or []:
                filters = ev.get("filter_keys") or {}
                filter_str = "; ".join(f"{k}: {v}" for k, v in filters.items())
                parts.append(
                    f"  event `{ev.get('event')}` ({ev.get('mechanism', 'polling')}): {ev.get('description', '')}"
                    + (f"\n    filter keys: {filter_str}" if filter_str else "")
                )
        if len(parts) == 1:
            return "No matching integration providers found."
        return "\n".join(parts)

    async def _get_integration(integration_id: str) -> str:
        from sqlalchemy import select

        from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
        from app.core.database import async_session as _builder_session
        from app.models.integration import Integration

        integ_uuid, id_err = await _id_arg(integration_id, "integration_id")
        if id_err:
            return id_err
        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            uid = None

        async with _builder_session() as _db:
            group_ids = await get_user_group_ids(_db, uid) if uid else []
            q = select(Integration).where(Integration.id == integ_uuid)
            q = q.where(caller_visible_filter(Integration, uid, group_ids))
            integ = (await _db.execute(q)).scalar_one_or_none()
            if not integ:
                return (
                    f"Integration `{integration_id}` not found or "
                    "you don't have access."
                )
            parts = [
                f"**{integ.name}**",
                f"  • ID: `{integ.id}`",
                f"  • Provider: {integ.provider}",
                f"  • Auth type: {integ.auth_type}",
                f"  • Status: {integ.status}",
                f"  • Description: {integ.description or 'None'}",
                f"  • Tags: {', '.join(integ.tags) if integ.tags else 'None'}",
                f"  • Company-managed: {'Yes' if integ.is_company_managed else 'No'}",
            ]
            # The suffix its tools carry when an agent binds several
            # integrations of this provider; update_integration sets it.
            if slug := getattr(integ, "tool_slug", None):
                parts.append(f"  • Tool suffix: `__{slug}`")
            # Which way an A2A partner relationship points (update_integration's
            # a2a_direction changes it); "none" names a row the repair missed.
            from app.a2a.providers import ALL_A2A_PROVIDERS, get_settings

            if integ.provider in ALL_A2A_PROVIDERS:
                a2a_row = await get_settings(_db, integ.id)
                parts.append(
                    f"  • A2A direction: {a2a_row.direction if a2a_row is not None else 'none (no settings row)'}"
                )
                # The outbound partner credential's state (#12346), never a secret.
                from app.a2a.client.credential_state import describe

                if (cred := describe(a2a_row, integ)) is not None:
                    parts.append(
                        f"  • Partner credential: {'usable' if cred['usable'] else 'NOT usable'}; "
                        f"last token minted at {cred['last_token_minted_at'] or 'never'}"
                    )
                    if err := cred["last_error"]:
                        parts.append(
                            f"  • Last credential error: {err['code']} at {err['at']}: {err['detail']} "
                            "An administrator must enter a valid client id and secret on this integration."
                        )
            # Whether its connected account is lent (update_integration's
            # share_connected_account sets it); on a per_user row, how many
            # people connected their own - a count, never who.
            if getattr(integ, "auth_type", None) == "oauth":
                from app.services.integration_user_credentials import (
                    connected_user_counts,
                    is_per_user,
                )

                connected = None
                if is_per_user(integ):
                    counts = await connected_user_counts(_db, [integ.id])
                    connected = counts.get(str(integ.id), 0)
                parts.append(f"  • Credential mode: {_credential_mode_line(integ, connected)}")
                parts.extend(_oauth_scope_lines(integ))
            shared = [str(g) for g in integ.shared_group_ids or []]
            if shared:
                from app.models.group import Group

                group_uuids = [u for g in shared if (u := safe_uuid(g))]
                group_rows = await _db.execute(
                    select(Group.id, Group.name).where(Group.id.in_(group_uuids))
                )
                group_names = {str(row[0]): row[1] for row in group_rows.all()}
                tiers = integ.shared_group_permissions or {}
                parts.append(
                    "  • Shared with groups: "
                    + ", ".join(
                        f"{group_names.get(g, 'unknown group')} (`{g}`, "
                        f"{_share_tier_label(tiers.get(g))})"
                        for g in shared
                    )
                )
            else:
                parts.append("  • Shared with groups: None")
            # Agents bound to it whose owner lost access: their runs refuse
            # its tools. Names only of the agents the caller can see.
            from app.models.agent import Agent
            from app.services.resource_exposure import orphaned_agents_of_integration

            orphans = await orphaned_agents_of_integration(_db, integ)
            if orphans:
                seen_rows = await _db.execute(
                    select(Agent.id).where(
                        Agent.id.in_([a.id for a in orphans]),
                        caller_visible_filter(Agent, uid, group_ids),
                    )
                )
                seen_ids = {str(i) for i in seen_rows.scalars().all()}
                named = [f"{a.name} (`{a.id}`)" for a in orphans if str(a.id) in seen_ids]
                hidden = len(orphans) - len(named)
                label = ", ".join(named + ([f"{hidden} you cannot see"] if hidden else []))
                parts.append(
                    f"  • Orphaned bindings: {len(orphans)} agent(s) whose owner no longer "
                    f"has access, their runs refuse its tools: {label}"
                )
            return "\n".join(parts)

    async def _create_integration(
        provider: str,
        name: str,
        auth_type: str = "",
        api_key: str = "",
        client_id: str = "",
        client_secret: str = "",
        extra_config: str = "",
        description: str = "",
        is_company_managed: bool = False,
        openapi: dict | str | None = None,
        tool_slug: str = "",
        share_connected_account: object = None,
        credential_mode: object = None,
        a2a_direction: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        from fastapi import HTTPException

        from app.a2a.providers import requested_direction
        from app.api.v1.integration_catalog import (
            PROVIDER_CATALOG,
            is_registration_only_provider,
            supported_auth_types,
            unsupported_auth_type_message,
        )
        from app.core.database import async_session as _builder_session
        from app.core.security import encrypt_value
        from app.models.integration import Integration
        from app.services.integration_credentials import apply_oauth_share_consent
        from app.services.integration_state import (
            admin_oauth_app,
            put_on_admin_oauth_app,
            takes_admin_oauth_app,
        )
        from app.services.openapi_import import builder_tools as spec_tools

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Integration creation is not available for anonymous users."
        name = (name or "").strip()
        if not name:
            return "Integration name is required (whitespace-only names are not allowed)."

        catalog = PROVIDER_CATALOG.get(provider)
        if not catalog:
            return (
                f"Unknown integration provider: '{provider}'. "
                "Use `list_integration_catalog` to see valid providers."
            )
        if is_registration_only_provider(provider):
            return (
                f"{provider} integrations are managed via platform registration "
                "and cannot be created manually."
            )
        # The REST create's rule: an A2A integration always has a direction,
        # inbound unless asked otherwise; any other provider takes none.
        try:
            direction = requested_direction(provider, a2a_direction if isinstance(a2a_direction, str) else None)
        except ValueError as exc:
            return f"Integration not created - {exc}"

        # A provider whose catalog entry declares ``spec_import`` is created from
        # an API spec, as the REST create does.
        spec_options, spec_err = await spec_tools.options_from_argument(openapi, file_context)
        if spec_err:
            return spec_err
        imports_spec = catalog.get("spec_import") == spec_tools.SPEC_IMPORT_KIND
        if spec_options is not None and not imports_spec:
            return spec_tools.wrong_provider(provider)
        if imports_spec and spec_options is None:
            return spec_tools.MISSING_SPEC

        effective_auth = auth_type or catalog.get("auth_type")
        # The REST create's rule (#8386): supported_auth_types, else exactly auth_type.
        supported = supported_auth_types(catalog)
        if not imports_spec and supported and effective_auth not in supported:
            return unsupported_auth_type_message(effective_auth, provider, supported)

        extra_obj, extra_err = _parse_json_object_arg(extra_config, "extra_config")
        if extra_err:
            return extra_err
        if effective_auth == "platform_managed" and (problem := _platform_base_url_refusal(extra_obj)):
            return f"Integration not created - {problem}"
        if direction is not None and extra_obj:
            # The REST create's pin check: an inbound federation config that
            # the A2A auth path would refuse is refused here, in front of the caller.
            from app.a2a.providers import federation_config_problem

            if problem := federation_config_problem(provider, direction, extra_obj):
                return f"Integration not created - {_as_sentence(problem)}"
        # The REST create's rule (#9898): a config given is complete for its provider.
        if extra_obj:
            from app.api.v1.integration_catalog import (
                missing_required_extra_fields,
                missing_required_extra_fields_message,
            )

            if missing := missing_required_extra_fields(provider, extra_obj):
                return f"Integration not created - {missing_required_extra_fields_message(provider, missing)}"
            # The REST create's typed-value rule (a domain list, a bounded number).
            from app.api.v1.integration_catalog import invalid_extra_fields, normalize_typed_extra_fields

            if invalid := invalid_extra_fields(provider, extra_obj):
                return f"Integration not created - {' '.join(invalid)}"
            extra_obj = normalize_typed_extra_fields(provider, extra_obj)
        if problem := secret_fields_problem(
            {"client_secret": client_secret, "api_key": api_key},
            {"client_id": client_id, "extra_config": extra_obj},
        ):
            return f"Integration not created - {problem}."
        wanted_slug, slug_err = _tool_slug_argument(tool_slug)
        if slug_err:
            return f"Integration not created - {slug_err}"
        share_choice, share_choice_err = _share_connected_account_argument(
            share_connected_account, credential_mode
        )
        if share_choice_err:
            return f"Integration not created - {share_choice_err}"

        async with _builder_session() as _db:
            caller = await _load_caller_user_for_builder(_db, uid)
            if caller is None:
                return "User not found."
            if is_company_managed and not caller.is_admin:
                return "Only admins can create company-managed integrations."
            if problem := await _vault_writer_refusal(
                uid, {"client_secret": client_secret, "api_key": api_key}, _db
            ):
                return f"Integration not created - {problem}."
            if problem := await variable_reference_problem(
                _db, {"client_secret": client_secret, "api_key": api_key}, viewer_ids=[uid]
            ):
                return f"Integration not created - {problem}."
            if direction is not None:
                # A2A is offered exactly while agentic chat is (the REST create's gate).
                from app.api.deps import require_chat_enabled

                try:
                    await require_chat_enabled(_db)
                except HTTPException as exc:
                    return f"Integration not created - {_as_sentence(str(exc.detail))}"

            scopes = catalog.get("scopes")
            authorize_url = catalog.get("authorize_url")
            token_url = catalog.get("token_url")
            spec_row = None
            if spec_options is not None:
                # The REST create's own steps: the admin switch, the spec-load
                # rate limit, the import, the base URL; the auth type is the
                # chosen option's, never the argument's.
                try:
                    spec_plan, spec_row, extra_obj = await spec_tools.plan_for_create(
                        _db, caller, spec_options, extra_obj
                    )
                except HTTPException as exc:
                    return f"Integration not created - {_as_sentence(str(exc.detail))}"
                effective_auth = spec_plan.auth_option.integration_auth_type
                oauth_defaults = spec_plan.auth_option.oauth_settings()
                scopes = oauth_defaults["scopes"]
                authorize_url = oauth_defaults["authorize_url"]
                token_url = oauth_defaults["token_url"]

            integ = Integration(
                user_id=uid,
                is_company_managed=is_company_managed,
                provider=provider,
                auth_type=effective_auth,
                name=name,
                description=(description or "").strip() or None,
                scopes=scopes,
                authorize_url=authorize_url,
                token_url=token_url,
            )
            if client_id:
                integ.client_id_enc = encrypt_value(client_id)
            if client_secret:
                integ.client_secret_enc = encrypt_value(client_secret)
            if api_key:
                integ.api_key_enc = encrypt_value(api_key)
            if extra_obj:
                integ.extra_config_enc = encrypt_value(json.dumps(extra_obj))
            if wanted_slug:
                # The REST update's check: unique per provider in the
                # owner scope. The id is set first so the count excludes none.
                from app.services.integration_tool_slugs import ToolSlugError, validate_tool_slug_change

                integ.id = uuid.uuid4()
                try:
                    integ.tool_slug = await validate_tool_slug_change(_db, integ, wanted_slug)
                except ToolSlugError as exc:
                    return f"Integration not created - {_as_sentence(str(exc))}"

            # Mirror the REST status rules.
            if effective_auth in ("api_key", "bot_token") and (api_key or client_id):
                integ.status = "connected"
            elif effective_auth == "none":
                has_extra_fields = bool(catalog.get("extra_fields"))
                if extra_obj or not has_extra_fields:
                    integ.status = "connected"
            elif effective_auth == "platform_managed":
                integ.status = "connected"
            elif effective_auth == "oauth" and client_id:
                integ.status = "configured"
            # The REST create's fallback, the same rule: an OAuth integration
            # created without its own client id holds its provider's admin app.
            on_admin_app = False
            if takes_admin_oauth_app(
                effective_auth, client_id_given=bool(client_id), is_company_managed=is_company_managed
            ):
                admin_app = await admin_oauth_app(_db, provider)
                if admin_app is not None:
                    put_on_admin_oauth_app(integ, admin_app)
                    on_admin_app = True
            if spec_row is not None:
                integ.status = spec_tools.status_after_create(integ, integ.status)
            # A company-managed OAuth integration shares the account it gets
            # connected with, as the UI's default choice does: its
            # creator is an admin, whose consent it is to give. The REST routes
            # keep the baseline for a request that makes no choice (decision 15c).
            # share_connected_account is the UI switch's choice, written by the
            # same function; the creator owns the row, so it is theirs to give.
            apply_oauth_share_consent(integ, share_credentials=share_choice, sharing_changed=True)

            _db.add(integ)
            try:
                if spec_row is not None:
                    await _db.flush()
                    spec_row.integration_id = integ.id
                    _db.add(spec_row)
                if direction is not None:
                    # Never an A2A integration without its settings row
                    # (the REST create's step): it would belong to neither
                    # direction, so it could neither mint nor delegate.
                    from app.a2a.providers import provision_settings

                    await _db.flush()
                    await provision_settings(_db, integ, direction)
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder create_integration failed for user %s", uid
                )
                await _db.rollback()
                return "Failed to create integration. Please try again."
            note = spec_tools.tools_line(spec_row) if spec_row is not None else ""
            if effective_auth == "oauth":
                note += (
                    "\n  • Ready to connect with the platform's OAuth app." if on_admin_app else ""
                ) + (
                    "\n  • OAuth integrations still need to be authorized from the "
                    "Integrations UI before they can be used."
                )
            slug_line = (
                f"\n  • Tool suffix: `__{integ.tool_slug}`" if getattr(integ, "tool_slug", None) else ""
            )
            if mode_line := _credential_mode_line(integ):
                note += f"\n  • Credential mode: {mode_line}"
            if share_choice is not None:
                note += _unapplied_share_choice_note(integ, share_choice)
            if direction is not None:
                note += f"\n  • A2A direction: {direction}"
            return (
                f"Integration **{name}** ({provider}) created successfully!\n"
                f"  • ID: `{integ.id}`\n"
                f"  • Status: {integ.status}{slug_line}{note}"
            )

    async def _update_integration(
        integration_id: str,
        name: str = "",
        description: str = "",
        auth_type: str = "",
        api_key: str = "",
        client_id: str = "",
        client_secret: str = "",
        scopes: str = "",
        authorize_url: str = "",
        token_url: str = "",
        extra_config: str = "",
        tags: str = "",
        shared_group_ids: str = "",
        shared_group_permissions: str = "",
        openapi: dict | str | None = None,
        tool_slug: str = "",
        share_connected_account: object = None,
        credential_mode: object = None,
        a2a_direction: str = "",
        is_company_managed: object = None,
        file_context: FileContext | None = None,
        **unknown_fields: object,
    ) -> str:
        from app.core.database import async_session as _builder_session
        from app.core.security import decrypt_json, encrypt_value
        from app.models.integration import Integration
        from app.services.integration_credentials import (
            ShareConsentRefusedError,
            may_consent_to_share,
        )
        from app.services.integration_state import (
            api_key_binding_of,
            forget_key_bound_elsewhere,
            repair_onto_admin_oauth_app,
            replace_oauth_client_id,
        )
        from app.services.integration_user_credentials import (
            apply_share_choice,
            delete_user_credentials,
            is_per_user,
            prune_unreachable_user_credentials,
        )

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Integration updates are not available for anonymous users."
        integ_uuid, id_err = _validate_id_arg(integration_id, "integration_id")
        if id_err:
            return id_err
        # A field this tool does not take is refused by name, never dropped.
        if unknown_fields:
            return f"Integration not updated - unsupported field(s): {', '.join(sorted(unknown_fields))}."
        want_cm, cm_err, cm_given = _parse_bool_arg(is_company_managed, "is_company_managed")
        if cm_err:
            return f"Integration not updated - {cm_err}"
        # Checked before anything is read or written.
        wanted_slug, slug_err = _tool_slug_argument(tool_slug)
        if slug_err:
            return f"Integration not updated - {slug_err}"
        share_choice, share_choice_err = _share_connected_account_argument(
            share_connected_account, credential_mode
        )
        if share_choice_err:
            return f"Integration not updated - {share_choice_err}"

        extra_obj, extra_err = _parse_json_object_arg(extra_config, "extra_config")
        if extra_err:
            return extra_err
        if problem := secret_fields_problem(
            {"client_secret": client_secret, "api_key": api_key},
            {"client_id": client_id, "extra_config": extra_obj},
        ):
            return f"Integration not updated - {problem}."
        share_ids, share_perms, share_err, sharing_given = _parse_group_sharing_args(
            shared_group_ids, shared_group_permissions
        )
        if share_err:
            return share_err
        from app.services.openapi_import import builder_tools as spec_tools

        spec_options, spec_err = await spec_tools.options_from_argument(openapi, file_context)
        if spec_err:
            return spec_err

        # Tool-call arguments are forwarded without type coercion, so a provider
        # can send JSON ``null`` for any string field. Canonicalise the plain
        # (non-secret) OAuth/auth fields up front so a ``null`` never reaches a
        # bare ``.strip()`` (which would raise ``AttributeError``) and a
        # whitespace-only value is treated as "leave unchanged". Credentials
        # (``api_key`` / ``client_id`` / ``client_secret``) are excluded — they
        # must be stored verbatim.
        auth_type = (auth_type or "").strip()
        scopes = (scopes or "").strip()
        authorize_url = (authorize_url or "").strip()
        token_url = (token_url or "").strip()
        if token_url_error := await _token_url_refusal(token_url):
            return token_url_error

        async with _builder_session() as _db:
            # Locked: the stored-reference check below and the write hold the row together.
            integ, perm_err = await _load_manageable_entity_for_builder(
                _db,
                Integration,
                uid,
                integ_uuid,
                action="edit",
                entity_label="integration",
                lock=True,
            )
            if perm_err is not None or integ is None:
                return perm_err or f"Integration `{integration_id}` not found."
            if getattr(integ, "is_builtin", False):
                return "Built-in integrations cannot be edited."
            # The REST update's rule: only an admin changes company-managed
            # status, refused before anything is written.
            cm_change = (
                cm_given and want_cm is not None and want_cm != bool(getattr(integ, "is_company_managed", False))
            )
            cm_caller = None
            if cm_change:
                cm_caller = await _load_caller_user_for_builder(_db, uid)
                if cm_caller is None or not getattr(cm_caller, "is_admin", False):
                    return (
                        "Integration not updated - only admins can change the company-managed status of an integration."
                    )
            # Direction first, as the REST update does: a refusal leaves the
            # integration untouched. Through change_direction, so a change is
            # refused while an agent binds it and clears the old credentials.
            wanted_direction = (a2a_direction or "").strip() if isinstance(a2a_direction, str) else ""
            direction_changed = False
            if wanted_direction:
                from fastapi import HTTPException

                from app.a2a.providers import (
                    ALL_A2A_PROVIDERS,
                    DirectionChangeRefusedError,
                    change_direction,
                    get_settings,
                    requested_direction,
                )
                from app.api.deps import require_chat_enabled

                if integ.provider not in ALL_A2A_PROVIDERS:
                    return "Integration not updated - a2a_direction applies only to A2A integrations."
                try:
                    requested_direction(integ.provider, wanted_direction)
                except ValueError as exc:
                    return f"Integration not updated - {exc}"
                try:
                    await require_chat_enabled(_db)
                except HTTPException as exc:
                    return f"Integration not updated - {_as_sentence(str(exc.detail))}"
                current = await get_settings(_db, integ.id)
                if current is None or current.direction != wanted_direction:
                    try:
                        await change_direction(_db, integ, wanted_direction)
                    except DirectionChangeRefusedError as exc:
                        await _db.rollback()
                        return f"Integration not updated - {exc}"
                    direction_changed = True
            # A spec replacement keeps the stored credential type (PUT
            # /{id}/openapi's rule): refused before any change, so the guard
            # compares the stored type, never one this call would set.
            if spec_options is not None and auth_type and auth_type != integ.auth_type:
                return (
                    "Integration not updated - an API spec replacement cannot be "
                    "combined with an auth_type change."
                )
            if problem := await _vault_writer_refusal(
                uid, {"client_secret": client_secret, "api_key": api_key}, _db
            ):
                return f"Integration not updated - {problem}."
            # Resolved with the owner's visibility at use time: both must see it.
            if problem := await variable_reference_problem(
                _db, {"client_secret": client_secret, "api_key": api_key}, viewer_ids=[uid, getattr(integ, "user_id", None)]
            ):
                return f"Integration not updated - {problem}."
            try:
                stored_extra = decrypt_json(integ.extra_config_enc) or {}
            except Exception:
                stored_extra = {}
            # The REST update's rule: a registration-only integration changes
            # only the keys its catalog lists; any other has the given keys merged
            # into its stored extra config (null removes one, a key left out is
            # kept), a secret sent back as the mask kept as stored.
            new_extra = None
            if extra_obj is not None:
                from fastapi import HTTPException

                from app.api.v1.integration_catalog import normalize_typed_extra_fields
                from app.api.v1.integrations import merged_extra_config

                try:
                    new_extra = await merged_extra_config(
                        _db, integ, normalize_typed_extra_fields(integ.provider, extra_obj)
                    )
                except HTTPException as exc:
                    return f"Integration not updated - {exc.detail}"
                # The REST update's rule (#9898): a write that would leave a
                # required key missing is refused before anything is written.
                from app.api.v1.integrations import required_extra_refusal

                if problem := required_extra_refusal(integ, new_extra):
                    return f"Integration not updated - {problem}"
                # The REST update's typed-value rule (a domain list, a bounded number).
                from app.api.v1.integration_catalog import invalid_extra_fields

                if invalid := invalid_extra_fields(integ.provider, extra_obj):
                    return f"Integration not updated - {' '.join(invalid)}"
            # Blank strings mean "leave unchanged" here.
            repoints = (new_extra is not None and new_extra != stored_extra) or any(
                value and value != getattr(integ, field)
                for field, value in (("authorize_url", authorize_url), ("token_url", token_url), ("auth_type", auth_type))
            )
            replaced = {column for column, value in (("api_key_enc", api_key), ("client_secret_enc", client_secret)) if value}
            # Cleared below only by a switch to platform_managed, never by an edit of one.
            if auth_type == "platform_managed" and integ.auth_type != "platform_managed":
                replaced |= {"api_key_enc", "client_secret_enc"}
            if problem := await _retained_reference_refusal(integ, uid, _db, repoints=repoints, replaced=replaced):
                return f"Integration not updated - {problem}."
            # Read before the edit can move the integration elsewhere.
            key_binding_before = api_key_binding_of(integ)

            updated: list[str] = []
            needs_authorization = False
            if (name or "").strip():
                integ.name = (name or "").strip()
                updated.append("name")
            if (description or "").strip():
                integ.description = (description or "").strip()
                updated.append("description")
            if wanted_slug and wanted_slug != getattr(integ, "tool_slug", None):
                # PUT /integrations/{id}'s path: the same uniqueness
                # check per provider and owner scope, its message as the answer.
                from app.services.integration_tool_slugs import ToolSlugError, validate_tool_slug_change

                try:
                    integ.tool_slug = await validate_tool_slug_change(_db, integ, wanted_slug)
                except ToolSlugError as exc:
                    return f"Integration not updated - {_as_sentence(str(exc))}"
                updated.append("tool_slug")
            if tags:
                integ.tags = [t.strip() for t in tags.split(",") if t.strip()]
                updated.append("tags")
            if client_id:
                needs_authorization = replace_oauth_client_id(integ, client_id)
                if needs_authorization and is_per_user(integ):
                    # A new OAuth app: the people's own grants were given to
                    # the old one.
                    await delete_user_credentials(_db, integ.id)
                updated.append("client_id")
            if client_secret:
                integ.client_secret_enc = encrypt_value(client_secret)
                updated.append("client_secret")
            if api_key:
                integ.api_key_enc = encrypt_value(api_key)
                if integ.auth_type in ("api_key", "bot_token"):
                    integ.status = "connected"
                updated.append("api_key")
            if new_extra is not None:
                # The REST update's A2A step: an outbound registration's card
                # URLs are its targets, so an edit of them adds and removes
                # the agents it can reach (discovered ones left alone).
                from app.a2a.providers import ALL_A2A_PROVIDERS as _A2A

                outbound_cards = None
                if integ.provider in _A2A:
                    from app.a2a.client.credentials import configured_card_urls
                    from app.a2a.providers import get_settings as _a2a_settings
                    from app.models.a2a import DIRECTION_OUTBOUND

                    _row = await _a2a_settings(_db, integ.id)
                    if _row is not None and _row.direction == DIRECTION_OUTBOUND:
                        outbound_cards = configured_card_urls(integ)
                integ.extra_config_enc = encrypt_value(json.dumps(new_extra))
                if outbound_cards is not None:
                    from app.a2a.client.credentials import configured_card_urls
                    from app.a2a.providers import reconcile_configured_targets

                    await reconcile_configured_targets(
                        _db, integ.id, previous=outbound_cards, current=configured_card_urls(integ)
                    )
                updated.append("extra_config")
            # OAuth endpoint fields are plain (non-secret) text; a
            # whitespace-only value is treated as "leave unchanged" and the
            # stored value is canonicalised to the stripped form.
            if authorize_url:
                integ.authorize_url = authorize_url
                updated.append("authorize_url")
            if token_url:
                integ.token_url = token_url
                updated.append("token_url")
            if scopes:
                integ.scopes = scopes
                updated.append("scopes")
            # Auth-type switching mirrors the REST ``update_integration``:
            # validate the new type against the provider catalog, and on a
            # switch to/from ``platform_managed`` clear stale credentials and
            # reset the connection status. The early return on an unsupported
            # type happens before the commit, so no partial change is persisted.
            if auth_type and auth_type != integ.auth_type:
                from app.api.v1.integration_catalog import (
                    PROVIDER_CATALOG,
                    supported_auth_types,
                    unsupported_auth_type_message,
                )

                new_auth = auth_type
                catalog = PROVIDER_CATALOG.get(integ.provider, {})
                supported = supported_auth_types(catalog)
                if supported and new_auth not in supported:
                    return unsupported_auth_type_message(new_auth, integ.provider, supported)
                if new_auth == "platform_managed" and (
                    problem := _platform_base_url_refusal(new_extra if new_extra is not None else stored_extra)
                ):
                    return f"Integration not updated - {problem}"
                old_auth = integ.auth_type
                integ.auth_type = new_auth
                # As in the REST update: a personal OAuth token must never
                # survive a switch away from "oauth", or it becomes
                # usable by everyone the integration is shared with.
                if old_auth == "oauth" and new_auth != "oauth":
                    integ.access_token_enc = None
                    integ.refresh_token_enc = None
                    integ.token_expires_at = None
                    integ.oauth_share_acknowledged = False
                    integ.oauth_token_owner_id = None
                    integ.uses_admin_oauth_credentials = False
                if new_auth == "platform_managed":
                    integ.api_key_enc = None
                    integ.access_token_enc = None
                    integ.refresh_token_enc = None
                    integ.client_secret_enc = None
                    integ.status = "connected"
                elif old_auth == "platform_managed":
                    integ.status = "not_connected"
                updated.append("auth_type")
            api_key_forgotten = forget_key_bound_elsewhere(
                integ, key_binding_before, new_key_given=bool(api_key)
            )

            # A new API spec: PUT /{id}/openapi's own path (the admin switch,
            # the rate limit, the same credential type, tool overrides carried,
            # OAuth endpoints followed).
            spec_row = None
            if spec_options is not None:
                spec_caller = await _load_caller_user_for_builder(_db, uid)
                if spec_caller is None:
                    return "User not found."
                spec_row, spec_refusal = await spec_tools.replace_for_update(
                    _db, integ, spec_caller, spec_options
                )
                if spec_refusal is not None:
                    return f"Integration not updated - {_as_sentence(spec_refusal)}"
                updated.append(spec_tools.SPEC_ARGUMENT)

            # The REST update's rule: an OAuth integration left without an app
            # gets its provider's admin OAuth app, as at creation, in place
            # (id, tool suffix and bindings kept); a client id given decides.
            if not client_id and await repair_onto_admin_oauth_app(_db, integ):
                updated.append("admin OAuth app")

            # Group sharing, the model agents use: members of a group
            # can bind and run the integration ("use", read_only) or also edit
            # it ("manage", config). The REST route's columns, written as it
            # writes them; the run-time rule that a shared agent only resolves
            # the bindings its user may use is ``shadow_invisible_bindings``.
            if sharing_given:
                share_err = await _apply_group_sharing(
                    _db, integ, share_ids, share_perms
                )
                if share_err:
                    return share_err
                updated.append("sharing")

            # Company-managed status: the REST update's write, through the
            # same helper (grants cleared going company-managed, agents of
            # other owners unbound going private).
            if cm_change and cm_caller is not None and want_cm is not None:
                from app.api.v1.integrations import apply_integration_company_managed

                await apply_integration_company_managed(_db, integ, want_cm, actor=cm_caller)
                updated.append("company-managed")

            # The connected-account choice, as the UI switch makes it: it
            # counts as a change of its own, so it can be the only argument.
            mode_before = getattr(integ, "credential_mode", None)
            acknowledged_before = getattr(integ, "oauth_share_acknowledged", None)
            if share_choice is not None:
                updated.append("share_connected_account")
            if direction_changed:
                updated.append("a2a_direction")

            if not updated:
                if wanted_slug:
                    return f"Integration **{integ.name}** already uses the tool suffix `__{wanted_slug}`."
                return "No fields to update. Provide at least one field."
            # The REST update's rule with the tool's implicit consent
            # (decision 15c keeps it for the tools, the REST routes need an
            # explicit choice): an edit never removes a shared OAuth
            # integration's token, and a switch to OAuth on a shared one shares
            # the account it is connected with - consented only by the account
            # holder (owner, recorded token owner, admin).
            _caller = await _load_caller_user_for_builder(_db, uid)
            # A switch away from OAuth, or a share that lends the account
            # again, disconnects the people's own accounts; a sharing
            # change, those of the people who no longer see it.
            # share_connected_account is the explicit choice the UI switch and
            # the REST body make, through the same function: true by an editor
            # who is not the account holder is refused before the commit.
            try:
                await apply_share_choice(
                    _db,
                    integ,
                    share_credentials=share_choice,
                    sharing_changed=bool({"auth_type", "sharing", "company-managed"} & set(updated)),
                    may_consent=may_consent_to_share(
                        integ, uid, is_admin=bool(getattr(_caller, "is_admin", False))
                    ),
                )
            except ShareConsentRefusedError as exc:
                await _db.rollback()
                return f"Integration not updated - {exc}"
            if is_per_user(integ) and (
                "sharing" in updated
                or "company-managed" in updated
                or mode_before != getattr(integ, "credential_mode", None)
            ):
                await _db.flush()
                await prune_unreachable_user_credentials(_db, integ)
            if share_choice is not None and (
                mode_before == getattr(integ, "credential_mode", None)
                and acknowledged_before == getattr(integ, "oauth_share_acknowledged", None)
                and updated == ["share_connected_account"]
            ):
                updated.remove("share_connected_account")
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder update_integration failed for integration %s",
                    integration_id,
                )
                await _db.rollback()
                return (
                    f"Failed to update integration `{integration_id}`. "
                    "Please try again."
                )
            # The agent accounts acting with it are checked again now, as
            # the REST update does, not at their next hourly resync (#9903).
            from app.services.agent_accounts.listening import sync_integration_accounts

            await sync_integration_accounts(str(integ.id))
            note = ""
            if needs_authorization and integ.auth_type == "oauth":
                note = (
                    "\n  • The client ID changed, so the tokens issued to the "
                    "previous OAuth app were cleared: authorize the integration "
                    "again from the Integrations UI before it is used."
                )
            if api_key_forgotten:
                note += (
                    "\n  • The API key was removed: a key belongs to the "
                    "address it was made for, and that address changed. Paste "
                    "a key made for the new address to use it again."
                )
            if "admin OAuth app" in updated:
                note += (
                    "\n  • Ready to connect with the platform's OAuth app: authorize "
                    "the integration from the Integrations UI."
                )
            if spec_row is not None:
                note = spec_tools.tools_line(spec_row) + note
            if "tool_slug" in updated:
                note = f"\n  • Tool suffix: `__{integ.tool_slug}`" + note
            if share_choice is not None:
                note += _unapplied_share_choice_note(integ, share_choice)
            if mode_line := _credential_mode_line(integ):
                note += f"\n  • Credential mode: {mode_line}"
            if new_extra is not None:
                note += await _stored_extra_config_line(_db, integ)
            if direction_changed:
                note += (
                    f"\n  • A2A direction: {wanted_direction}; the credentials and "
                    "targets of the previous direction were cleared."
                )
            # Never echo credentials — only report which fields changed.
            changed = ", ".join(updated) or "nothing (the connected-account choice was already so)"
            return f"Integration **{integ.name}** updated: {changed}.{note}"

    async def _delete_integration(integration_id: str) -> str:
        from sqlalchemy import select

        from app.core.database import async_session as _builder_session
        from app.models.agent import Agent
        from app.models.integration import Integration

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Integration deletion is not available for anonymous users."
        integ_uuid, id_err = _validate_id_arg(integration_id, "integration_id")
        if id_err:
            return id_err

        async with _builder_session() as _db:
            integ, perm_err = await _load_manageable_entity_for_builder(
                _db,
                Integration,
                uid,
                integ_uuid,
                action="delete",
                entity_label="integration",
            )
            if perm_err is not None or integ is None:
                return perm_err or f"Integration `{integration_id}` not found."
            if getattr(integ, "is_builtin", False):
                return "Built-in integrations cannot be deleted."
            i_name = integ.name
            iid = str(integ.id)
            # Purge stale references from all agents' integration_bindings
            # (parity with REST DELETE /integrations/{id}).
            agents_result = await _db.execute(select(Agent))
            for agent in agents_result.scalars().all():
                bindings = agent.integration_bindings or {}
                if any(iid in ids for ids in bindings.values()):
                    cleaned = {
                        k: [v for v in ids if v != iid] for k, ids in bindings.items()
                    }
                    agent.integration_bindings = {
                        k: v for k, v in cleaned.items() if v
                    }
            await _db.delete(integ)
            try:
                await _db.commit()
            except Exception:
                logger.exception(
                    "Agent builder delete_integration failed for integration %s",
                    integration_id,
                )
                await _db.rollback()
                return (
                    f"Failed to delete integration `{integration_id}`. "
                    "Please try again."
                )
            return f"Integration **{i_name}** has been deleted."

    async def _import_agent_memory(agent_id: str, entries: object = None) -> str:
        """Recreate exported persistent memory entries on an agent."""
        from pydantic import TypeAdapter, ValidationError

        from app.api.v1.agent_memory import (
            ImportMemoryEntry,
            import_scope_error,
            unknown_user_ids,
        )
        from app.core.database import async_session as _builder_session
        from app.services.persistent_memory import (
            MEMORY_IMPORT_MAX_ENTRIES,
            MemoryImportEntry,
            MemoryImportError,
            import_memories,
        )

        try:
            uid = uuid.UUID(user_id)
        except (ValueError, AttributeError):
            return "Memory import is not available for anonymous users."
        agent_uuid, id_err = _validate_id_arg(agent_id, "agent_id")
        if id_err:
            return id_err
        if isinstance(entries, str):
            try:
                entries = json.loads(entries)
            except ValueError:
                return "`entries` must be a JSON list of memory entries."
        if not isinstance(entries, list) or not entries:
            return "`entries` must be a non-empty list of memory entries."
        if len(entries) > MEMORY_IMPORT_MAX_ENTRIES:
            return (
                f"At most {MEMORY_IMPORT_MAX_ENTRIES} entries per call; "
                f"got {len(entries)}. Split the import."
            )
        try:
            parsed = TypeAdapter(list[ImportMemoryEntry]).validate_python(entries)
        except ValidationError as exc:
            return f"Invalid memory entries: {exc.errors()[0].get('msg', exc)}"

        async with _builder_session() as _db:
            agent, perm_err = await _load_writable_agent_for_builder(
                _db, uid, agent_uuid, action="modify the memory of"
            )
            if perm_err is not None or agent is None:
                return perm_err or f"Agent `{agent_id}` not found."
            caller = await _load_caller_user_for_builder(_db, uid)
            if caller is None:
                return "User not found."
            user_ids = {e.user_id for e in parsed if e.user_id is not None}
            scope_err = import_scope_error(agent, caller, user_ids)
            if scope_err:
                return scope_err
            unknown = await unknown_user_ids(_db, user_ids)
            if unknown:
                return "Unknown user_id: " + ", ".join(sorted(str(u) for u in unknown))
            try:
                created = await import_memories(
                    _db,
                    agent_id=agent.id,
                    entries=[MemoryImportEntry(**e.model_dump()) for e in parsed],
                )
                await _db.commit()
            except MemoryImportError as exc:
                await _db.rollback()
                return f"Nothing imported: {exc}"
            return (
                f"Imported {len(created)} memory entries into agent "
                f"**{agent.name}** (`{agent.id}`)."
            )

    # Register all functions
    executor.register("list_agent_templates", _list_agent_templates)
    executor.register("list_available_resources", _list_available_resources)
    executor.register("create_agent", _create_agent)
    executor.register("clone_agent", _clone_agent)
    executor.register("list_integration_tools", _list_integration_tools)
    executor.register("run_agent_once", _run_agent_once)
    executor.register("create_assignment", _add_assignment_to_agent)
    executor.register("add_assignment_to_agent", _add_assignment_to_agent)
    executor.register("get_agent", _get_agent)
    executor.register("list_agents", _list_agents)
    executor.register("list_groups", _list_groups)
    executor.register("update_agent", _update_agent)
    executor.register("delete_agent", _delete_agent)
    executor.register("list_assignments", _list_assignments)
    executor.register("get_assignment", _get_assignment)
    executor.register("update_assignment", _update_assignment)
    executor.register("delete_assignment", _delete_assignment)
    executor.register("trigger_assignment", _trigger_assignment_tool)
    executor.register("list_runs", _list_runs)
    executor.register("cancel_runs", _cancel_runs)
    executor.register("get_run", _get_run)
    executor.register("list_knowledge_bases", _list_knowledge_bases)
    executor.register("create_knowledge_base", _create_knowledge_base)
    executor.register("add_kb_document", _add_kb_document)
    executor.register("list_custom_tools", _list_custom_tools)
    executor.register("get_custom_tool", _get_custom_tool)
    executor.register("create_custom_tool", _create_custom_tool)
    executor.register("update_custom_tool", _update_custom_tool)
    executor.register("delete_custom_tool", _delete_custom_tool)
    executor.register("list_integrations", _list_integrations)
    executor.register("list_integration_catalog", _list_integration_catalog)
    executor.register("list_platform_events", _list_platform_events)
    executor.register("watch_platform_event", _watch_platform_event)
    executor.register("list_platform_event_watches", _list_platform_event_watches)
    executor.register("cancel_platform_event_watch", _cancel_platform_event_watch)
    executor.register("get_integration", _get_integration)
    executor.register("create_integration", _create_integration)
    executor.register("update_integration", _update_integration)
    executor.register("delete_integration", _delete_integration)
    executor.register("import_agent_memory", _import_agent_memory)
    executor.register("list_prompts", _list_prompts)
    executor.register("get_prompt", _get_prompt)
    executor.register("create_prompt", _create_prompt)
    executor.register("update_prompt", _update_prompt)
    executor.register("delete_prompt", _delete_prompt)
    executor.register("list_skills", _list_skills)
    executor.register("get_skill", _get_skill)
    executor.register("create_skill", _create_skill)
    executor.register("update_skill", _update_skill)
    executor.register("delete_skill", _delete_skill)
    executor.register("list_variables", _list_variables)
    executor.register("get_variable", _get_variable)
    executor.register("create_variable", _create_variable)
    executor.register("update_variable", _update_variable)
    executor.register("delete_variable", _delete_variable)
    executor.register("list_mcp_servers", _list_mcp_servers)
    executor.register("get_mcp_server", _get_mcp_server)
    executor.register("create_mcp_server", _create_mcp_server)
    executor.register("update_mcp_server", _update_mcp_server)
    executor.register("delete_mcp_server", _delete_mcp_server)
    executor.register("get_knowledge_base", _get_knowledge_base)
    executor.register("update_knowledge_base", _update_knowledge_base)
    executor.register("delete_knowledge_base", _delete_knowledge_base)
    executor.register("list_kb_documents", _list_kb_documents)
    executor.register("get_kb_document", _get_kb_document)
    executor.register("delete_kb_document", _delete_kb_document)

    from app.services.openapi_import import builder_tools as _spec_tools

    definitions = [
        ToolDefinition(
            name="list_agent_templates",
            description=(
                f"List all available agent templates in the {_platform_label()} platform. "
                "Templates provide pre-configured agent setups with default personas "
                "and assignments for common use cases (Mail Desk, Support Relay, "
                "Channel Monitor, News Monitor, etc.). Use this to help users choose "
                "a template when creating a new agent."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_available_resources",
            description=(
                "List resources available to the user that can be attached to agents: "
                "connected integrations (email, calendar, GitHub, Slack, etc.), "
                "knowledge bases, MCP tool servers, and custom tools. Use this to "
                "discover what resources exist before creating an agent, so you can "
                "bind the right integrations and knowledge bases."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "resource_type": {
                        "type": "string",
                        "description": (
                            "Filter by resource type: 'all', 'integrations', "
                            "'knowledge_bases', 'mcp_servers', or 'custom_tools'. Default: 'all'."
                        ),
                        "default": "all",
                    },
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="create_agent",
            description=(
                f"Create a new AI agent in the {_platform_label()} platform, "
                "optionally from a template (which adds its default assignments); "
                "it is available at once for chat and assignments. Accepts every "
                "field update_agent accepts, so one call builds a complete agent; "
                "if any field is refused, nothing is created. The result lists "
                "every integration binding as provider:id (name)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the agent (e.g. 'Email Assistant', 'Code Reviewer')",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short description of what the agent does",
                        "default": "",
                    },
                    "persona": {
                        "type": "string",
                        "description": (
                            "The agent's persona / system prompt. Defines its behavior, "
                            "expertise, tone, and instructions. If using a template, this "
                            "overrides the template's default persona. Leave empty to use "
                            "the template's persona."
                        ),
                        "default": "",
                    },
                    "template_slug": {
                        "type": "string",
                        "description": (
                            "Optional template slug to create the agent from (e.g. "
                            "'email-triage', 'teams-support'). Use list_agent_templates "
                            "to see available templates."
                        ),
                        "default": "",
                    },
                    "icon": {
                        "type": "string",
                        "description": "Lucide icon name for the agent (e.g. 'Bot', 'Mail', 'MessageSquare', 'Shield'). Default: 'Bot'",
                        "default": "Bot",
                    },
                    "knowledge_base_ids": {
                        "type": "string",
                        "description": "Comma-separated knowledge base IDs to attach (use list_available_resources to find IDs)",
                        "default": "",
                    },
                    "knowledge_base_permissions": {
                        "type": "string",
                        "description": (
                            "Per-KB write access as 'KB_ID:write' / 'KB_ID:read' "
                            "pairs, comma-separated. Bound KBs are read-only by "
                            "default; a 'write' grant adds the store_knowledge tool "
                            "for that KB. The KB must also be in knowledge_base_ids "
                            "(unbound grants are ignored). Example: 'UUID:write'"
                        ),
                        "default": "",
                    },
                    "mcp_server_ids": {
                        "type": "string",
                        "description": "Comma-separated MCP server IDs to attach",
                        "default": "",
                    },
                    "custom_tool_ids": {
                        "type": "string",
                        "description": "Comma-separated custom tool IDs to attach",
                        "default": "",
                    },
                    "integration_bindings": {
                        "type": "string",
                        "description": (
                            "Integration bindings as 'provider:id' pairs, comma-separated. "
                            "Example: 'microsoft:abc-123,github:def-456'. Use list_available_resources "
                            "to find integration IDs and their providers."
                        ),
                        "default": "",
                    },
                    "create_default_assignments": {
                        "type": "boolean",
                        "description": "If using a template, whether to create its default assignments. Default: true",
                        "default": True,
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags for the agent (e.g. 'email,support,triage')",
                        "default": "",
                    },
                    "is_company_managed": {
                        "type": "boolean",
                        "description": (
                            "Make the agent company-managed: shared platform-wide "
                            "and read-only for non-admins (admin only). "
                            "Company-managed agents may only bind company-managed "
                            "resources. Default: false."
                        ),
                        "default": False,
                    },
                    "wait_policy": {
                        "type": ["object", "string", "null"],
                        "description": (
                            "Per-agent wait policy: {per_call_seconds, total_seconds, "
                            "unbounded_with_until}, any subset, narrowing the platform AI settings "
                            "(never above). An assignment's own wins."
                        ),
                    },
                    "max_iterations": {
                        "type": ["integer", "null"],
                        "description": (
                            "Waits are bounded apart, by wait_policy (platform values: "
                            "get_platform_ai_settings). "
                            "Per-agent cap on the agentic loop (max LLM/tool "
                            f"rounds per chat message / assignment run, 1-{MAX_ITERATIONS_CEILING}). "
                            "Omit or null = use the platform default. A run still "
                            "making progress at its cap keeps going, and long "
                            "visual work (videos, decks) needs more rounds. An "
                            "assignment can still override this per-run."
                        ),
                        "default": None,
                    },
                },
                "required": ["name"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="clone_agent",
            description=(
                "Personal copy of an agent you can see, with overrides. Copies the "
                "rest (model, memory flags, scoping, deny list, read-only bindings, "
                "handover); never assignments, sharing, reserved tags; memory only "
                "with copy_memory. A binding you may not use is left out, reported."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source_agent_id": {
                        "type": "string",
                        "description": "UUID of the agent to copy",
                    },
                    "name": {
                        "type": "string",
                        "description": "Name of the copy",
                    },
                    "persona": {
                        "type": "string",
                        "description": "Replacement persona (empty: the source's)",
                        "default": "",
                    },
                    "persona_append": {
                        "type": "string",
                        "description": "Text appended to the source's persona",
                        "default": "",
                    },
                    "description": {
                        "type": "string",
                        "description": "Replacement description",
                        "default": "",
                    },
                    "add_integration_bindings": {
                        "type": "string",
                        "description": "'provider:integration_id' pairs to bind, comma-separated",
                        "default": "",
                    },
                    "remove_integration_bindings": {
                        "type": "string",
                        "description": "'provider:integration_id' pairs to unbind, comma-separated",
                        "default": "",
                    },
                    "add_knowledge_base_ids": {
                        "type": "string",
                        "description": "Knowledge base UUIDs to attach, comma-separated",
                        "default": "",
                    },
                    "remove_knowledge_base_ids": {
                        "type": "string",
                        "description": "Knowledge base UUIDs to detach, comma-separated",
                        "default": "",
                    },
                    "add_builtin_tool_ids": {
                        "type": "string",
                        "description": (
                            "Built-in tools to enable, comma-separated "
                            f"({', '.join(_advertised_builtin_tool_ids())})"
                        ),
                        "default": "",
                    },
                    "remove_builtin_tool_ids": {
                        "type": "string",
                        "description": "Built-in tools to disable, comma-separated",
                        "default": "",
                    },
                    "copy_memory": {
                        "type": "boolean",
                        "description": (
                            "Also copy the persistent memory you may read (dates, types, subjects, superseded kept)"
                        ),
                        "default": False,
                    },
                },
                "required": ["source_agent_id", "name"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="run_agent_once",
            description=(
                "Run an agent once on a prompt (smoke test) in its own run and "
                "sandbox workspace. Waits a minute, else returns a task_id to "
                "call again with. Agents you can edit only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "UUID of the agent to run",
                        "default": "",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "What the agent does",
                        "default": "",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "A running run's task_id to collect",
                        "default": "",
                    },
                    "file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Attached as in a chat message, at most 30",
                    },
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_integration_tools",
            description=(
                "The tools of an integration provider, each read or write. A "
                "read-only binding (integration_read_only) keeps the read tools. "
                "Several integrations of one provider on an agent: each tool gets "
                "the integration's tool suffix (list_integrations shows it). A "
                "provider of imported API specs lists each visible integration's "
                "operations with access, HTTP method and path; integration_id "
                "lists one."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": (
                            "Provider key of the integration catalog "
                            "(list_integration_catalog lists them)"
                        ),
                    },
                    "integration_id": {
                        "type": "string",
                        "description": (
                            "One integration's id instead of a provider "
                            "(list_integrations shows it): an integration "
                            "with an imported API spec lists its operations"
                        ),
                    },
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="create_assignment",
            description=(
                "Add a new assignment to an existing agent. Assignments are automation rules "
                "that define when and how an agent should act. Each assignment has a trigger "
                "(schedule, webhook, integration_event, manual, or work_item), an action prompt that "
                "instructs the agent what to do, optional gates that decide whether to "
                "run (activation_check: a read-only tool call tested with no model call; "
                "then activation_prompt), and execution settings (human-in-the-loop, "
                "AI label, notifications, digest, dry run, loop protection, retries, and "
                "the iteration / timeout limits). Use this after create_agent to add "
                "custom assignments beyond the template defaults."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The ID of the agent to add the assignment to",
                    },
                    "name": {
                        "type": "string",
                        "description": "Name of the assignment (e.g. 'Daily email triage', 'Auto-reply to mentions')",
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The action prompt for the assignment. This tells the agent "
                            "what to do when the assignment triggers. Be specific and detailed."
                        ),
                    },
                    "activation_prompt": {
                        "type": "string",
                        "description": (
                            "Optional activation gate prompt - a YES/NO pre-filter evaluated "
                            "BEFORE the action prompt that decides whether the assignment should "
                            "run at all (e.g. 'Is this a GitHub pull_request opened event from a "
                            "non-bot sender? Answer YES or NO only.'). Leave empty (or pass the "
                            "literal 'none') for no gate, so the assignment always runs; any "
                            "other value is stored verbatim as the gate prompt."
                        ),
                        "default": "",
                    },
                    "trigger_type": {
                        "type": "string",
                        "description": (
                            "How the assignment is triggered: 'schedule' (cron), 'webhook' (HTTP POST), "
                            "'integration_event' (e.g. new email), 'manual' (user-triggered), or "
                            "'work_item' (the agent's inbox: each open work item it owns is "
                            "claimed and run, one at a time), or 'platform_event' (an event "
                            "inside the platform, see list_platform_events). Default: 'manual'"
                        ),
                        "default": "manual",
                    },
                    "trigger_config": {
                        "type": "string",
                        "description": (
                            "JSON string with trigger configuration. For schedule: "
                            '\'{"cron": "0 9 * * 1-5"}\' (weekdays at 9am). For webhook: '
                            '\'{"path": "my-hook"}\'. For integration_event: '
                            '\'{"provider": "microsoft", "event": "new_email"}\', plus an '
                            'optional "filter" object evaluated before any run or gate '
                            "(a dropped event creates no run and is counted). Each "
                            "provider's events and the filter keys each one takes are "
                            "listed by list_integration_catalog (available_events). "
                            'For work_item: \'{}\', or \'{"correlation_prefix": "dev:", '
                            '"priority_min": "high"}\' to take only some items; '
                            '"max_concurrent_runs": N (1 to 10, default 1) runs up to N '
                            "items in parallel, urgent first then oldest. "
                            'For platform_event: \'{"event": "work_item.note_added", '
                            '"filter": {"correlation_key": "x"}}\', with "once": true '
                            "to wait for one matching event only. "
                            "Leave empty for manual triggers."
                        ),
                        "default": "",
                    },
                    "activation_check": _ACTIVATION_CHECK_SCHEMA,
                    "description": {
                        "type": "string",
                        "description": "Short description of the assignment",
                        "default": "",
                    },
                    "delay": {
                        "type": "string",
                        "description": "Optional delay before execution (e.g. '10m', '1h'). Useful for 'wait then check' patterns.",
                        "default": "",
                    },
                    "human_in_the_loop": {
                        "type": ["boolean", "null"],
                        "description": (
                            "Whether the assignment can pause and ask a human for "
                            "input. Omit or pass null to inherit from the agent "
                            "(the default); true / false force it on / off."
                        ),
                        "default": None,
                    },
                    "human_in_the_loop_prompt": {
                        "type": "string",
                        "description": (
                            "Optional custom human-in-the-loop prompt override. "
                            "Leave empty (or pass the literal 'none') to use the "
                            "platform default."
                        ),
                        "default": "",
                    },
                    "ai_label": {
                        "type": "boolean",
                        "description": (
                            "If true, outgoing messages are labelled as "
                            "AI-generated. Default: true"
                        ),
                        "default": True,
                    },
                    "ai_label_prompt": {
                        "type": "string",
                        "description": (
                            "Optional custom AI-label prompt override. Leave "
                            "empty (or pass the literal 'none') to use the "
                            "platform default."
                        ),
                        "default": "",
                    },
                    "notify_on_complete": {
                        "type": "boolean",
                        "description": (
                            "If true, send the user a notification when the run "
                            "finishes. Default: false"
                        ),
                        "default": False,
                    },
                    "show_in_digest": {
                        "type": "boolean",
                        "description": (
                            "If true, show the result in 'Latest Results' on the "
                            "dashboard (for digests, reports, analyses). "
                            "Default: false"
                        ),
                        "default": False,
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": (
                            "If true, the assignment logs what it would do "
                            "without taking real actions (tool calls are "
                            "intercepted and logged). Default: false"
                        ),
                        "default": False,
                    },
                    "ignore_self_triggered_events": {
                        "type": "boolean",
                        "description": (
                            "If true, integration events caused by the agent's "
                            "own writes are dropped before they create a run "
                            "(loop protection). Default: false"
                        ),
                        "default": False,
                    },
                    "retry_on_failure": {
                        "type": "boolean",
                        "description": (
                            "If true, automatically re-execute the run when it "
                            "fails (up to max_retries). Default: false"
                        ),
                        "default": False,
                    },
                    "max_retries": {
                        "type": "integer",
                        "description": (
                            "Number of retry attempts before giving up when "
                            "retry_on_failure is true (0-10). Default: 3"
                        ),
                        "default": 3,
                    },
                    "wait_policy": {
                        "type": ["object", "string", "null"],
                        "description": (
                            "Per-assignment wait policy: {per_call_seconds, total_seconds, "
                            "unbounded_with_until}, any subset; wins over the agent's, "
                            "bounded by the platform."
                        ),
                    },
                    "organization_context_mode": {"enum": ["inherit", "always", "off"]},
                    "organization_context_categories": {"type": ["array", "string"]},
                    "max_iterations": {
                        "type": ["integer", "null"],
                        "description": (
                            "Per-run cap on the agentic loop (max LLM/tool turns, "
                            "1-100). Omit or null = inherit (the agent's "
                            "max_iterations, then the platform default). A value "
                            "OVERRIDES the agent's setting for this assignment."
                        ),
                        "default": None,
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": (
                            "Maximum wall-clock time for a single run in seconds "
                            "(0-3600; 0 disables the timeout). Default: 300"
                        ),
                        "default": 300,
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "Whether the assignment is active. Default: true",
                        "default": True,
                    },
                },
                "required": ["agent_id", "name", "prompt"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_agent",
            description=(
                f"Get an agent's configuration in the {_platform_label()} platform, "
                "every editable option (bindings as 'provider:id' with names, "
                "scoping, prompts, skills, handover, memory, channels, sharing, "
                "persona). Read it BEFORE editing one option."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The UUID of the agent to retrieve",
                    },
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Only these, identity always shown (default all): behavior, model, tools, "
                            "handover, memory, human_in_the_loop, channels, sharing, guardrails, intents, persona"
                        ),
                    },
                },
                "required": ["agent_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_agents",
            description=(
                f"List agents in the {_platform_label()} platform (names, IDs, "
                "enabled status, assignment counts), paginated, the agents "
                "visible to the user by default; scope='all' (admins) lists "
                "every agent. detail='full' adds owner, creation time, "
                "integration bindings, knowledge bases and web/channel "
                "exposure per agent: use it for a bulk audit instead of one "
                "get_agent per agent."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": (
                            "Optional search filter on agent name "
                            "(case-insensitive substring match)"
                        ),
                        "default": "",
                    },
                    "scope": {
                        "type": "string",
                        "description": (
                            "Listing scope: 'visible' (default — agents you "
                            "own, shared with your groups, or company-"
                            "managed) or 'all' (ADMIN ONLY — every agent on "
                            "the platform, with owner shown). Only use "
                            "'all' when the user explicitly asks for a "
                            "platform-wide view."
                        ),
                        "default": "visible",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Page size (1-200). Default: 50. Use with "
                            "offset to page through large platforms."
                        ),
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "Number of agents to skip (for pagination). "
                            "Default: 0."
                        ),
                        "default": 0,
                    },
                    "detail": {
                        "type": "string",
                        "description": (
                            "Detail level: 'summary' (default — name, id, "
                            "enabled status, assignment count) or 'full' "
                            "(adds owner, created timestamp, integration "
                            "bindings with generic provider names, "
                            "knowledge base names, and web/channel "
                            "exposure). Use 'full' for bulk audits / "
                            "reconciliation to avoid one get_agent call "
                            "per agent."
                        ),
                        "default": "summary",
                    },
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_groups",
            description=(
                f"List the user groups of the {_platform_label()} platform: "
                "name, id, description, member count, and whether the user you "
                "act for belongs to each. Use it to find the group id to share "
                "an agent or an integration with (update_agent shared_group_ids, "
                "update_integration), and to say who an existing group share "
                "reaches. members='true' also lists each group's members by "
                "name and email - for administrators only; everyone else gets "
                "the member count. To share with named people outside any "
                "group, use update_agent users=... with their emails instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": (
                            "Optional filter on the group name "
                            "(case-insensitive substring match)."
                        ),
                        "default": "",
                    },
                    "members": {
                        "type": "string",
                        "description": (
                            "'true' to list the members of each group "
                            "(administrators only; others see the count)."
                        ),
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Page size (1-200). Default: 50.",
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of groups to skip (for pagination). Default: 0.",
                        "default": 0,
                    },
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_runs",
            description=(
                f"List assignment runs (executions) in the "
                f"{_platform_label()} platform, most recent first, "
                "paginated. Each entry shows the run id, agent, assignment, "
                "status, timing, and a result/error preview. By default "
                "lists runs visible to the current user (own runs + "
                "company-managed runs); admins can pass scope='all' to "
                "list every run on the platform. Filter by agent, "
                "assignment, status, or a search string. Use this to "
                "answer questions like 'what happened in the last run?' "
                "or 'show me recent failures'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "Optional agent id (or its first 8+ hex "
                            "characters) — only runs of this agent"
                        ),
                        "default": "",
                    },
                    "assignment_id": {
                        "type": "string",
                        "description": (
                            "Optional assignment id (or its first 8+ hex "
                            "characters) — only runs of this assignment"
                        ),
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "description": (
                            "Optional status filter. One of: "
                            + ", ".join(
                                f"'{s}'" for s in _run_status_vocabulary()
                            )
                        ),
                        "default": "",
                    },
                    "search": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive search across agent "
                            "name, assignment name, result summary, error, "
                            "trigger payload (webhook/event bodies — use "
                            "this to check whether a run was ever triggered "
                            "for a given item), and actions taken. Does NOT "
                            "search the conversation log — use get_run with "
                            "`search` for that. If a search finds nothing, "
                            "do not retry many variants: filter by "
                            "agent_id/assignment_id instead, or conclude "
                            "the event never triggered a run."
                        ),
                        "default": "",
                    },
                    "scope": {
                        "type": "string",
                        "description": (
                            "Listing scope: 'visible' (default — your own "
                            "runs + company-managed runs) or 'all' (ADMIN "
                            "ONLY — every run on the platform). Only use "
                            "'all' when the user explicitly asks for a "
                            "platform-wide view."
                        ),
                        "default": "visible",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Page size (1-200). Default: 20.",
                        "default": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "Number of runs to skip (for pagination). "
                            "Default: 0."
                        ),
                        "default": 0,
                    },
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="cancel_runs",
            description=(
                "ADMIN ONLY. Cancel, in one call, every assignment run that has "
                "not started yet (status pending, delayed or queued) of an "
                "agent and / or an assignment, optionally only those created "
                "before a date-time. Use it to clear a backlog, e.g. the queued "
                "runs of an agent whose quota is exhausted. Cancelled runs keep "
                "their history with status 'cancelled' and the reason "
                "'Cancelled by' followed by your name; running runs are never touched. Pass "
                "dry_run=true first to get the count without changing anything."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Agent UUID: only runs of this agent.",
                        "default": "",
                    },
                    "assignment_id": {
                        "type": "string",
                        "description": "Assignment UUID: only runs of this assignment.",
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "description": (
                            "Comma-separated statuses among 'pending', "
                            "'delayed', 'queued'. Default: all three."
                        ),
                        "default": "",
                    },
                    "created_before": {
                        "type": "string",
                        "description": (
                            "ISO-8601 date-time: only runs created before it."
                        ),
                        "default": "",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "true: count the matching runs, cancel nothing.",
                        "default": False,
                    },
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_run",
            description=(
                "Get the full details of a single assignment run "
                "(execution): agent, assignment, status, trigger, timing, "
                "token usage, error, human-in-the-loop exchange, and the "
                "COMPLETE result summary. Use after list_runs to inspect a "
                "specific run — e.g. to quote what an agent produced in "
                "its last run. Pass `search` to grep INSIDE the run "
                "(conversation log, trigger payload, actions, result, "
                "error) and get matching excerpts — the way to answer "
                "'did this run touch item X?'. Admins can open any run; "
                "other users can open their own runs and company-managed "
                "runs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": (
                            "The id of the run to retrieve, or its first 8+ "
                            "hex characters"
                        ),
                    },
                    "search": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive term to search for "
                            "inside the run's full content (result summary, "
                            "error, trigger payload/context, actions taken, "
                            "human-input exchange, and the complete "
                            "conversation log). Returns matching excerpts "
                            "with surrounding context instead of the full "
                            "run details."
                        ),
                        "default": "",
                    },
                },
                "required": ["run_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="update_agent",
            description=(
                f"Edit ANY of an agent's configuration options in the "
                f"{_platform_label()} platform — full parity with the agent "
                "settings UI (identity, persona, guardrails, behavior, model, "
                "tools & scoping, handover, memory, human-in-the-loop, channels "
                "& web, visibility/sharing). Only agents you own (or can manage) "
                "can be edited. Provide only the fields you want to change; "
                "unspecified / blank fields remain unchanged. Most clearable "
                "fields accept 'none'/'clear' to reset to the default. Binding "
                "fields (knowledge_base_ids, mcp_server_ids, custom_tool_ids, "
                "prompt_ids, skill_ids, handover_agent_ids, integration_bindings, "
                "users) REPLACE the current selection by default; pass "
                "binding_mode='add' or 'remove' to change a single binding "
                "without re-sending the whole selection. Sharing: shared_group_ids "
                "shares with groups (list_groups names them), users with "
                "individual people by email. To change part of a long persona "
                "or guardrails, never resend it whole: mode 'edit' (exact "
                "find/replace), 'append', 'replace_section' or 'insert_after' "
                "on 'field' (persona, guardrails, description); the answer "
                "carries the diff. ALWAYS call get_agent first to "
                "read the exact current configuration."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The UUID of the agent to update",
                    },
                    "name": {
                        "type": "string",
                        "description": "New name for the agent",
                        "default": "",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description for the agent",
                        "default": "",
                    },
                    "persona": {
                        "type": "string",
                        "description": ("New persona / system prompt for the agent"),
                        "default": "",
                    },
                    "enabled": {
                        "type": "string",
                        "description": (
                            "Set to 'true' or 'false' to enable/disable " "the agent"
                        ),
                        "default": "",
                    },
                    "tags": {
                        "type": "string",
                        "description": (
                            "Comma-separated tags (replaces existing tags)"
                        ),
                        "default": "",
                    },
                    "icon": {
                        "type": "string",
                        "description": "New Lucide icon name (e.g. 'Bot', 'Mail')",
                        "default": "",
                    },
                    "output_format": {
                        "type": "string",
                        "description": (
                            "Response output format: 'markdown', 'html', "
                            "'json', or 'plain_text'"
                        ),
                        "default": "",
                    },
                    "llm_model": {
                        "type": "string",
                        "description": (
                            "LLM model override, or 'none' to clear the "
                            "override and use the platform default. Must be "
                            "one of the platform's configured available "
                            "models — never invent a model id; omit this "
                            "field unless the user asked for a specific model"
                        ),
                        "default": "",
                    },
                    "llm_provider": {
                        "type": "string",
                        "description": (
                            "LLM provider key to pin the agent to (e.g. "
                            "'anthropic', 'openai', 'gemini'). 'none'/'clear' "
                            "resets to auto-resolution and also clears "
                            "llm_provider_id."
                        ),
                        "default": "",
                    },
                    "llm_provider_id": {
                        "type": "string",
                        "description": (
                            "UUID of a specific configured provider row to pin "
                            "the agent to (disambiguates when several providers "
                            "share a model). 'none'/'clear' clears it."
                        ),
                        "default": "",
                    },
                    "llm_model_source": {
                        "type": "string",
                        "description": (
                            "'routing' runs the agent on the platform routing "
                            "model (the administrator's lightweight model) and "
                            "drops its own model - for a narrow, repeated "
                            "decision a small model handles well; 'none' returns "
                            "it to its own model. Choosing llm_model also leaves "
                            "the routing model."
                        ),
                        "default": "",
                    },
                    "knowledge_base_ids": {
                        "type": "string",
                        "description": (
                            "Comma-separated knowledge base UUIDs to attach "
                            "(REPLACES the current set; use list_knowledge_bases "
                            "for IDs)"
                        ),
                        "default": "",
                    },
                    "knowledge_base_permissions": {
                        "type": "string",
                        "description": (
                            "Per-KB write access as 'KB_ID:write' or "
                            "'KB_ID:read' pairs, comma-separated (REPLACES the "
                            "current grants). A bound KB is read-only by default; "
                            "a 'write' grant gives the agent the store_knowledge "
                            "tool for that KB. The KB must also be in "
                            "knowledge_base_ids (unbound grants are ignored). Use "
                            "'none'/'clear' to reset all KBs to read-only. "
                            "Example: 'UUID:write'"
                        ),
                        "default": "",
                    },
                    "mcp_server_ids": {
                        "type": "string",
                        "description": (
                            "Comma-separated MCP server UUIDs to attach "
                            "(REPLACES the current set)"
                        ),
                        "default": "",
                    },
                    "custom_tool_ids": {
                        "type": "string",
                        "description": (
                            "Comma-separated custom tool UUIDs to attach "
                            "(REPLACES the current set)"
                        ),
                        "default": "",
                    },
                    "prompt_ids": {
                        "type": "string",
                        "description": (
                            "Comma-separated prompt UUIDs to inject into the "
                            "system prompt (REPLACES the current set)"
                        ),
                        "default": "",
                    },
                    "skill_ids": {
                        "type": "string",
                        "description": (
                            "Comma-separated skill UUIDs to inject into the "
                            "system prompt (REPLACES the current set)"
                        ),
                        "default": "",
                    },
                    "integration_bindings": {
                        "type": "string",
                        "description": (
                            "Integration bindings as 'provider:id' pairs, "
                            "comma-separated. REPLACES the current set by "
                            "default; with binding_mode='add'/'remove' the "
                            "given pairs are merged into / subtracted from the "
                            "current bindings, so you can attach or detach a "
                            "single integration without re-sending the rest. "
                            "Example: 'microsoft:abc-123,github:def-456'"
                        ),
                        "default": "",
                    },
                    "handover_agent_ids": {
                        "type": "string",
                        "description": (
                            "Comma-separated agent UUIDs this agent can hand "
                            "over to (REPLACES the current set)"
                        ),
                        "default": "",
                    },
                    "is_company_managed": {
                        "type": "string",
                        "description": (
                            "Set to 'true' or 'false' to change the agent's "
                            "company-managed (platform-wide visibility) status. "
                            "Admin only. Making an agent company-managed strips "
                            "its personal (non-company-managed) bindings."
                        ),
                        "default": "",
                    },
                    "wait_policy": {
                        "type": ["object", "string", "null"],
                        "description": (
                            "Per-agent wait policy: {per_call_seconds, total_seconds, "
                            "unbounded_with_until}, any subset, narrowing the platform AI settings "
                            "(never above). 'none' clears it; blank leaves it."
                        ),
                    },
                    "max_iterations": {
                        "type": ["string", "integer"],
                        "description": (
                            "Waits are bounded apart, by wait_policy (platform values: "
                            "get_platform_ai_settings). "
                            "Per-agent cap on the agentic loop (max LLM/tool "
                            "rounds per chat message / assignment run, 1-100), "
                            "as an integer. Use 'none' / 'default' / 'inherit' "
                            "to clear the override and fall back to the platform "
                            "default. Omit / blank = leave unchanged. An "
                            "assignment can still override this per-run."
                        ),
                        "default": "",
                    },
                    "slug": {
                        "type": "string",
                        "description": (
                            "New URL slug (lowercase, hyphenated; must be "
                            "unique). Changing it alters the agent's webhook + "
                            "public URLs."
                        ),
                        "default": "",
                    },
                    "guardrails": {
                        "type": "string",
                        "description": (
                            "Safety constraints always injected into the system "
                            "prompt (separate from the persona). 'none'/'clear' "
                            "removes them."
                        ),
                        "default": "",
                    },
                    "output_schema": {
                        "type": "string",
                        "description": (
                            "JSON Schema (as a JSON string) for structured "
                            "output when output_format='json'. 'none'/'clear' "
                            "removes it."
                        ),
                        "default": "",
                    },
                    "temperature": {
                        "type": ["string", "number"],
                        "description": (
                            "LLM temperature override (0.0-2.0). Use "
                            "'none'/'default'/'inherit' to clear and use the "
                            "provider default."
                        ),
                        "default": "",
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "description": (
                            "Reasoning effort for thinking models: 'low', "
                            "'medium', 'high', or 'max'. Use 'none' to clear."
                        ),
                        "default": "",
                    },
                    "organization_context_mode": {"enum": ["inherit", "always", "off"]},
                    "organization_context_categories": {"type": ["array", "string"]},
                    "builtin_tool_ids": {
                        "type": "string",
                        "description": (
                            "Comma-separated built-in groups "
                            f"({', '.join(_advertised_builtin_tool_ids())}); "
                            "follows binding_mode; 'none'/'clear' disables all."
                        ),
                        "default": "",
                    },
                    "tool_deny_list": {
                        "type": "string",
                        "description": (
                            "Comma-separated tool names to ALWAYS block (deny "
                            "wins over any allow list), across all sources. "
                            "REPLACES the list; 'none'/'clear' removes all "
                            "denials."
                        ),
                        "default": "",
                    },
                    "integration_template_bindings": {
                        "type": "string",
                        "description": (
                            "Comma-separated integration provider keys resolved "
                            "per-user at runtime (e.g. 'opencti,openaev'). "
                            "REPLACES the list; 'none'/'clear' empties it."
                        ),
                        "default": "",
                    },
                    "integration_allowed_tools": {
                        "type": "string",
                        "description": (
                            "JSON object {provider: [tool_name, ...]} restricting "
                            "which integration tools are available (null/omitted "
                            "per provider = all). An integration imported from an "
                            "API spec is its own API: a list under its provider "
                            "restricts only the integrations whose tools it names "
                            "(stored as 'PROVIDER@INTEGRATION_ID' keys). "
                            "'none'/'clear' removes scoping."
                        ),
                        "default": "",
                    },
                    "mcp_allowed_tools": {
                        "type": "string",
                        "description": (
                            "JSON object {mcp_server_id: [tool_name, ...]} "
                            "restricting which MCP tools are available. "
                            "'none'/'clear' removes scoping."
                        ),
                        "default": "",
                    },
                    "builtin_allowed_tools": {
                        "type": "string",
                        "description": (
                            "JSON object {builtin_group_id: [function, ...]} "
                            "keeping only those functions of a built-in group "
                            "(null/omitted per group = all). An unknown function "
                            "is refused with the group's valid ones. "
                            "'none'/'clear' removes scoping."
                        ),
                        "default": "",
                    },
                    "integration_element_bindings": {
                        "type": "string",
                        "description": (
                            "JSON object {'provider:element_type': [element, "
                            "...]} scoping the agent to specific integration "
                            "sub-elements (repos, channels). 'none'/'clear' "
                            "removes scoping."
                        ),
                        "default": "",
                    },
                    "integration_element_enforcement": {
                        "type": "string",
                        "description": (
                            "Comma-separated element types whose "
                            "integration_element_bindings selection is a hard "
                            "boundary on the tools instead of guidance: "
                            "github:repository, slack:slack_channel, "
                            "microsoft:teams_channel, hubspot:crm_object_type. "
                            "A type without a selection is ignored. REPLACES "
                            "the list; 'none'/'clear' makes every selection "
                            "guidance."
                        ),
                        "default": "",
                    },
                    "integration_read_only": {
                        "type": "string",
                        "description": (
                            "Bound integrations the agent may only READ, as "
                            "'provider:integration_id' pairs (each in "
                            "integration_bindings): write tools withheld and "
                            "refused. Follows binding_mode; 'none'/'clear' "
                            "clears."
                        ),
                        "default": "",
                    },
                    "integration_template_read_only": {
                        "type": "string",
                        "description": (
                            "Same for integration_template_bindings: provider "
                            "keys (each template-bound). REPLACES; 'none' clears."
                        ),
                        "default": "",
                    },
                    "integration_live_writes": {
                        "type": "string",
                        "description": (
                            "Live writes per binding, for integrations that offer "
                            "it: JSON {integration id or provider key: "
                            "'on'|'off'|'inherit'}, merged."
                        ),
                        "default": "",
                    },
                    "integration_signatures": {
                        "type": "string",
                        "description": (
                            "Email signature overrides: JSON {integration id: "
                            '{"signature_mode": "inherit"|"on"|"off", '
                            '"signature_integration_id"?}} or just the mode; '
                            "merged per binding, inherit removes one."
                        ),
                        "default": "",
                    },
                    "handover_types": {
                        "type": "string",
                        "description": (
                            "Per-target handover type as 'AGENT_ID:TYPE' "
                            "pairs, comma-separated. Types: 'delegate' "
                            "(default), 'consult', 'transfer', "
                            "'mandatory_transfer'. 'none'/'clear' resets. Only "
                            "agents in handover_agent_ids are kept."
                        ),
                        "default": "",
                    },
                    "handover_conditions": {
                        "type": "string",
                        "description": (
                            "JSON object {agent_id: condition} routing "
                            "conditions for 'mandatory_transfer' targets. "
                            "'none'/'clear' resets."
                        ),
                        "default": "",
                    },
                    "prepare_tool_ref": {
                        "type": "string",
                        "description": (
                            "JSON object referencing a no-arg tool to run BEFORE "
                            "the LLM loop (pipeline). 'none'/'clear' removes it."
                        ),
                        "default": "",
                    },
                    "finalize_tool_ref": {
                        "type": "string",
                        "description": (
                            "JSON object referencing a tool to run AFTER the LLM "
                            "loop to post-process the response. 'none'/'clear' "
                            "removes it."
                        ),
                        "default": "",
                    },
                    "skip_llm": {
                        "type": "string",
                        "description": (
                            "'true'/'false'. When true the LLM loop is skipped "
                            "and the prepare tool output flows to finalize."
                        ),
                        "default": "",
                    },
                    "memory_short_term_enabled": {
                        "type": "string",
                        "description": "'true'/'false' — Redis 24h rolling memory.",
                        "default": "",
                    },
                    "memory_medium_term_enabled": {
                        "type": "string",
                        "description": (
                            "'true'/'false' — Redis 30d consolidated summary."
                        ),
                        "default": "",
                    },
                    "memory_persistent_enabled": {
                        "type": "string",
                        "description": (
                            "'true'/'false' — durable PostgreSQL fact memory."
                        ),
                        "default": "",
                    },
                    "memory_user_enabled": {
                        "type": "string",
                        "description": (
                            "'true'/'false' — cross-agent per-user memory."
                        ),
                        "default": "",
                    },
                    "human_in_the_loop": {
                        "type": "string",
                        "description": (
                            "'true'/'false' — agent-level HITL default "
                            "(assignments inherit unless they override)."
                        ),
                        "default": "",
                    },
                    "human_in_the_loop_prompt": {
                        "type": "string",
                        "description": (
                            "Custom HITL instruction prompt. 'none'/'clear' "
                            "resets to the default."
                        ),
                        "default": "",
                    },
                    "available_channel_types": {
                        "type": "string",
                        "description": (
                            "Comma-separated channels that may route to this "
                            "agent (e.g. 'web,slack'). Admin-only. 'none'/'clear' "
                            "= all channels."
                        ),
                        "default": "",
                    },
                    "channel_config": {
                        "type": "string",
                        "description": (
                            "JSON {channel_type: {channel_bot_id, receive_enabled, "
                            "publish_enabled, ...}}, merged into the stored map: null "
                            "drops a key or channel, 'none'/'clear' drops all."
                        ),
                        "default": "",
                    },
                    "web_enabled": {
                        "type": "string",
                        "description": (
                            "'true'/'false' — expose the agent via the public "
                            "embed widget (auto-generates a web token)."
                        ),
                        "default": "",
                    },
                    "web_suggested_prompts": {
                        "type": "string",
                        "description": (
                            "Newline-separated suggested prompts for the web "
                            "widget greeting. 'none'/'clear' removes them."
                        ),
                        "default": "",
                    },
                    "is_public_catalog": {
                        "type": "string",
                        "description": (
                            "'true'/'false' - publish to the Filigran AI "
                            "Catalog (company-managed agent, admin only)."
                        ),
                        "default": "",
                    },
                    "catalog_include_personal_tools": {
                        "type": "string",
                        "description": (
                            "'true'/'false' - on publishes the code of the "
                            "user's personal custom tools in the public catalog download."
                        ),
                        "default": "",
                    },
                    "disable_chat": {
                        "type": "string",
                        "description": (
                            "'true'/'false' - hide the agent from chat and the "
                            "LLM proxy."
                        ),
                        "default": "",
                    },
                    "allow_self_fanout": {
                        "type": "string",
                        "description": (
                            "'true'/'false' - let the agent split a large "
                            "workload across background copies of itself "
                            "(on by default)."
                        ),
                        "default": "",
                    },
                    "accept_caller_output_format": {
                        "type": "string",
                        "description": (
                            "'true'/'false' - accept a caller's output_schema "
                            "(default off)."
                        ),
                        "default": "",
                    },
                    "shared_group_ids": {
                        "type": "string",
                        "description": (
                            "Comma-separated Group UUIDs to share this agent with "
                            "(full read/write for members). 'none'/'clear' "
                            "unshares."
                        ),
                        "default": "",
                    },
                    "shared_group_permissions": {
                        "type": "string",
                        "description": (
                            "JSON object {group_id: permission} for per-group "
                            "access. 'none'/'clear' resets."
                        ),
                        "default": "",
                    },
                    "users": {
                        "type": ["array", "string"],
                        "items": {"type": "string"},
                        "description": (
                            "Share the agent with individual people, by email "
                            "address or platform user id (a list, or "
                            "comma-separated) - for people outside any group. "
                            "Follows binding_mode: 'replace' (default) makes them "
                            "the agent's per-user audience, 'add' gives them access "
                            "(or changes the access of someone already shared "
                            "with), 'remove' takes their access away at once. "
                            "'none'/'clear' removes every per-user share. Only "
                            "existing accounts: an address no account matches is "
                            "refused by name and nobody is invited. A person the "
                            "agent is shared with uses, in their chats, only the "
                            "integrations they may use themselves, as a group "
                            "member does. A company-managed agent is already open "
                            "to everyone and is not shared this way. The result "
                            "and get_agent name who has access."
                        ),
                        "default": "",
                    },
                    "users_permission": {
                        "type": "string",
                        "description": (
                            "The access given to the people in users: 'read_only' "
                            "(default - see and chat with the agent), 'config' "
                            "(also edit it) or 'config_with_chats' (also read its "
                            "chats). Ignored with binding_mode 'remove'."
                        ),
                        "default": "",
                    },
                    "config": {
                        "type": "string",
                        "description": (
                            "Free-form agent config as a JSON object. "
                            "'none'/'clear' resets."
                        ),
                        "default": "",
                    },
                    "binding_mode": {
                        "type": "string",
                        "description": (
                            "How the binding fields are applied: 'replace' "
                            "(default — the given value REPLACES the current "
                            "set), 'add' (merge the given ids/pairs INTO the "
                            "current set, deduped), or 'remove' (subtract the "
                            "given ids/pairs from the current set). Applies to "
                            "knowledge_base_ids, mcp_server_ids, "
                            "custom_tool_ids, prompt_ids, skill_ids, "
                            "handover_agent_ids, integration_bindings, "
                            "builtin_tool_ids, and the people in users. Use "
                            "'add'/'remove' to change a single binding without "
                            "re-sending the whole set. The 'none'/'clear' "
                            "sentinels only apply in 'replace' mode."
                        ),
                        "default": "replace",
                    },
                    **_partial_text_schema(AGENT_PARTIAL_TEXT_FIELDS),
                },
                "required": ["agent_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="delete_agent",
            description=(
                f"Delete an agent and all its assignments from the "
                f"{_platform_label()} platform. Only agents you own can be "
                "deleted. This action cannot be undone. Its conversations are "
                "kept with their history; name a successor_agent_id to have them "
                "continue with that agent (for the people who can use it)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The UUID of the agent to delete",
                    },
                    "successor_agent_id": {
                        "type": "string",
                        "description": (
                            "Optional: the UUID of an enabled agent you can use that "
                            "takes over the deleted agent's conversations."
                        ),
                        "default": "",
                    },
                },
                "required": ["agent_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_assignments",
            description=(
                "List assignments visible to the current user. Optionally "
                "filter by agent ID. Returns assignment names, IDs, trigger "
                "types, and enabled status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": ("Optional agent UUID to filter assignments by"),
                        "default": "",
                    },
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_assignment",
            description=(
                "Get one assignment: name, trigger and its configuration, delay, "
                "enabled, every execution setting (HITL, AI label, notify, digest, "
                "dry run, ignore-self-triggered, retries, max_iterations, timeout), "
                "the gate stages (tool check, then prompt gate), for an "
                "integration_event trigger its delivery, last poll and filtered "
                "count, and the FULL prompts: activation gate, action, AI-label "
                "and HITL overrides."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assignment_id": {
                        "type": "string",
                        "description": ("The UUID of the assignment to retrieve"),
                    },
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Only these, settings and trigger always shown (default all): gates, "
                            "activation_prompt, prompt, ai_label_prompt, human_in_the_loop_prompt"
                        ),
                    },
                },
                "required": ["assignment_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="update_assignment",
            description=(
                "Update an assignment you can edit (built-in agents are read-only). Provide "
                "only the fields you want to change. This exposes the full "
                "assignment configuration surface: the tool-check gate "
                "(activation_check) and the activation gate prompt (each set "
                "independently of the action prompt), the human-in-the-loop "
                "ON/OFF toggle (distinct from its prompt override), the "
                "AI-label, notify-on-complete, show-in-digest, dry-run, "
                "ignore-self-triggered-events and retry-on-failure toggles, and "
                "the max_retries / max_iterations / timeout_seconds limits. To "
                "change part of a long prompt, never resend it whole: mode "
                "'edit' (exact find/replace), 'append', 'replace_section' or "
                "'insert_after' on 'field' (prompt, activation_prompt, "
                "description); the answer carries the diff."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assignment_id": {
                        "type": "string",
                        "description": ("The UUID of the assignment to update"),
                    },
                    "name": {
                        "type": "string",
                        "description": "New name for the assignment",
                        "default": "",
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "New action prompt (the main instruction executed "
                            "when the gate passes)"
                        ),
                        "default": "",
                    },
                    "activation_prompt": {
                        "type": "string",
                        "description": (
                            "New activation gate prompt — the YES/NO pre-filter "
                            "evaluated BEFORE the action prompt that decides "
                            "whether the assignment should run at all. This is a "
                            "separate field from 'prompt'. Pass 'none' to remove "
                            "the gate (the assignment then always runs)."
                        ),
                        "default": "",
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "New short description. Pass 'none' to clear it."
                        ),
                        "default": "",
                    },
                    "ai_label_prompt": {
                        "type": "string",
                        "description": (
                            "Custom AI-label prompt override. Pass 'none' to "
                            "reset to the platform default."
                        ),
                        "default": "",
                    },
                    "human_in_the_loop_prompt": {
                        "type": "string",
                        "description": (
                            "Custom human-in-the-loop prompt override. Pass "
                            "'none' to reset to the platform default."
                        ),
                        "default": "",
                    },
                    "trigger_type": {
                        "type": "string",
                        "description": (
                            "New trigger type: 'schedule', 'webhook', "
                            "'integration_event', 'manual', 'work_item' (the "
                            "agent's inbox: each open work item it owns is claimed "
                            "and run, one at a time), or 'platform_event' (see "
                            "list_platform_events)"
                        ),
                        "default": "",
                    },
                    "trigger_config": {
                        "type": "string",
                        "description": (
                            "New trigger configuration as a JSON string. An "
                            "integration_event trigger may carry a \"filter\" object; "
                            "list_integration_catalog lists each event and its filter keys. "
                            "A work_item trigger takes '{}' or the optional "
                            "correlation_prefix / priority_min keys, and "
                            "max_concurrent_runs (1 to 10, default 1) to run up to "
                            "that many items in parallel."
                        ),
                        "default": "",
                    },
                    "fix_applied": {
                        "type": "string",
                        "description": "PR or item whose fix this trigger_config applies: friction recorded disposed",
                    },
                    "activation_check": _ACTIVATION_CHECK_SCHEMA,
                    "delay": {
                        "type": "string",
                        "description": (
                            "New delay before execution (e.g. '10m', '1h'), "
                            "or 'none' to remove"
                        ),
                        "default": "",
                    },
                    "enabled": {
                        "type": ["string", "boolean"],
                        "description": (
                            "Set to 'true' or 'false' to enable/disable "
                            "the assignment"
                        ),
                        "default": "",
                    },
                    "human_in_the_loop": {
                        "type": ["string", "boolean"],
                        "description": (
                            "The human-in-the-loop ON/OFF toggle (NOT the prompt "
                            "override). Set to 'true' or 'false', or "
                            f"'{INHERIT_VALUE}' to clear the override so it "
                            "inherits from the agent."
                        ),
                        "default": "",
                    },
                    "ai_label": {
                        "type": ["string", "boolean"],
                        "description": (
                            "Set to 'true' or 'false' to label outgoing messages "
                            "as AI-generated."
                        ),
                        "default": "",
                    },
                    "notify_on_complete": {
                        "type": ["string", "boolean"],
                        "description": (
                            "Set to 'true' or 'false' to send the user a "
                            "notification when the run finishes."
                        ),
                        "default": "",
                    },
                    "show_in_digest": {
                        "type": ["string", "boolean"],
                        "description": (
                            "Set to 'true' or 'false' to show the result in "
                            "'Latest Results' on the dashboard."
                        ),
                        "default": "",
                    },
                    "dry_run": {
                        "type": ["string", "boolean"],
                        "description": (
                            "Set to 'true' or 'false'. When true, the assignment "
                            "logs what it would do without taking real actions."
                        ),
                        "default": "",
                    },
                    "ignore_self_triggered_events": {
                        "type": ["string", "boolean"],
                        "description": (
                            "Set to 'true' or 'false' to drop integration events "
                            "caused by the agent's own writes (loop protection)."
                        ),
                        "default": "",
                    },
                    "retry_on_failure": {
                        "type": ["string", "boolean"],
                        "description": (
                            "Set to 'true' or 'false' to automatically "
                            "re-execute the run when it fails."
                        ),
                        "default": "",
                    },
                    "max_retries": {
                        "type": ["string", "integer"],
                        "description": (
                            "Number of retry attempts before giving up when "
                            "retry_on_failure is true, as an integer (0-10)."
                        ),
                        "default": "",
                    },
                    "wait_policy": {
                        "type": ["object", "string", "null"],
                        "description": (
                            "Per-assignment wait policy: {per_call_seconds, total_seconds, "
                            "unbounded_with_until}, any subset; wins over the agent's, never above "
                            "the platform AI settings. 'none' clears it; blank leaves it."
                        ),
                    },
                    "organization_context_mode": {"enum": ["inherit", "always", "off"]},
                    "organization_context_categories": {"type": ["array", "string"]},
                    "max_iterations": {
                        "type": ["string", "integer"],
                        "description": (
                            "Per-run cap on the agentic loop (max LLM/tool turns, "
                            "1-100), as an integer. A value OVERRIDES the agent's "
                            "setting for this assignment. Use 'none' / 'inherit' "
                            "/ 'default' to clear it and inherit (agent override "
                            "-> platform default). Omit / blank = leave unchanged."
                        ),
                        "default": "",
                    },
                    "timeout_seconds": {
                        "type": ["string", "integer"],
                        "description": (
                            "Maximum wall-clock time for a single run in "
                            "seconds, as an integer (0-3600; 0 disables "
                            "the timeout)."
                        ),
                        "default": "",
                    },
                    **_partial_text_schema(ASSIGNMENT_PARTIAL_TEXT_FIELDS),
                },
                "required": ["assignment_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="delete_assignment",
            description=(
                "Delete an assignment. You can delete any assignment you have "
                "permission to edit - those on your own agents, plus "
                "assignments on company-managed or group-shared agents you can "
                "manage (built-in agents are read-only). This action cannot be "
                "undone. While a run of it is in flight (yours included) it is "
                "refused, unless after_run=true: paused now, deleted once its "
                "runs end."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assignment_id": {
                        "type": "string",
                        "description": ("The UUID of the assignment to delete"),
                    },
                    "after_run": {
                        "type": "boolean",
                        "description": "Defer the deletion to the end of its runs in flight.",
                    },
                },
                "required": ["assignment_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="trigger_assignment",
            description=(
                "Manually trigger an assignment to execute immediately. "
                "The assignment and its agent must both be enabled. "
                "Optionally provide a JSON payload as trigger context. "
                "Returns the run id, which wait's `until` accepts; the "
                "conversation is told when the run ends."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assignment_id": {
                        "type": "string",
                        "description": ("The UUID of the assignment to trigger"),
                    },
                    "payload": {
                        "type": "string",
                        "description": (
                            "Optional JSON payload to pass as trigger context"
                        ),
                        "default": "",
                    },
                },
                "required": ["assignment_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_knowledge_bases",
            description=(
                f"List all knowledge bases visible to the current user in "
                f"the {_platform_label()} platform. Returns names, IDs, "
                "document counts, and chunk counts."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="create_knowledge_base",
            description=(
                f"Create a new knowledge base in the {_platform_label()} "
                "platform. A knowledge base stores vectorized documents "
                "for RAG (retrieval-augmented generation). After creation, "
                "add documents using `add_kb_document`."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the knowledge base",
                    },
                    "description": {
                        "type": "string",
                        "description": ("Optional description of the knowledge base"),
                        "default": "",
                    },
                    "is_company_managed": {
                        "type": "boolean",
                        "description": (
                            "Make the knowledge base company-managed: shared "
                            "platform-wide and read-only for non-admins (admin "
                            "only). Default: false."
                        ),
                        "default": False,
                    },
                },
                "required": ["name"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="add_kb_document",
            description=(
                "Add a text document to an existing knowledge base. "
                "The document will be automatically chunked and embedded "
                "for semantic search. Cannot add to company-managed "
                "knowledge bases."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "knowledge_base_id": {
                        "type": "string",
                        "description": (
                            "The UUID of the knowledge base to add the " "document to"
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": ("Title / filename for the document"),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "The text content of the document "
                            "(markdown or plain text)"
                        ),
                    },
                },
                "required": ["knowledge_base_id", "title", "content"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_custom_tools",
            description=(
                f"List all custom tools visible to the current user in "
                f"the {_platform_label()} platform. Returns tool names, "
                "IDs, descriptions, and enabled status."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_custom_tool",
            description=(
                "Get detailed information about a specific custom tool. "
                "Returns name, description, language, input schema, and "
                "enabled status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": ("The UUID of the custom tool to retrieve"),
                    },
                },
                "required": ["tool_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_integrations",
            description=(
                f"List all integrations visible to the current user in "
                f"the {_platform_label()} platform. Returns integration "
                "names, IDs, providers, and connection status."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            source="builtin:platform",
        ),
        # ── Custom tools (create / update / delete) ───────────────────
        ToolDefinition(
            name="create_custom_tool",
            description=(
                "Create a custom tool — a user-defined function (JavaScript or "
                "Python) the agent can call. Provide a JSON Schema describing the "
                "inputs and the function body. The tool runs in a sandbox."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name"},
                    "description": {
                        "type": "string",
                        "description": "What the tool does (shown to the LLM)",
                        "default": "",
                    },
                    "js_function": {
                        "type": "string",
                        "description": (
                            "The function body. JS: inputs are available as "
                            "`$variable_name`. Python: as `variable_name`."
                        ),
                        "default": "",
                    },
                    "input_schema": {
                        "type": "string",
                        "description": (
                            "JSON Schema object string defining the inputs, e.g. "
                            '\'{"type":"object","properties":{"keyword":'
                            '{"type":"string"}},"required":["keyword"]}\''
                        ),
                        "default": "",
                    },
                    "language": {
                        "type": "string",
                        "description": "'javascript' (default) or 'python'",
                        "default": "javascript",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags",
                        "default": "",
                    },
                    "is_company_managed": {
                        "type": "boolean",
                        "description": "Share with all users (admin only)",
                        "default": False,
                    },
                },
                "required": ["name"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="update_custom_tool",
            description=(
                "Update an existing custom tool. Provide only the fields to "
                "change. Creator or admin only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": "The UUID of the custom tool to update",
                    },
                    "name": {"type": "string", "default": ""},
                    "description": {"type": "string", "default": ""},
                    "js_function": {"type": "string", "default": ""},
                    "input_schema": {
                        "type": "string",
                        "description": "JSON Schema object string",
                        "default": "",
                    },
                    "language": {
                        "type": "string",
                        "description": "'javascript' or 'python'",
                        "default": "",
                    },
                    "tags": {"type": "string", "default": ""},
                    "enabled": {
                        "type": "string",
                        "description": "'true' or 'false'",
                        "default": "",
                    },
                },
                "required": ["tool_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="delete_custom_tool",
            description=(
                "Delete a custom tool and unbind it from all agents. Creator or "
                "admin only. This action cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tool_id": {
                        "type": "string",
                        "description": "The UUID of the custom tool to delete",
                    },
                },
                "required": ["tool_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_platform_events",
            description=(
                "List the platform events an assignment with trigger_type "
                "'platform_event' can subscribe to (work item notes, status "
                "changes, claims, runs failed or orphaned, background tasks "
                "ended), with the payload keys each one's filter takes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Optional case-insensitive filter",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="watch_platform_event",
            description=(
                "Set a one-off watch on a platform event that wakes a BACKGROUND TASK of an agent, "
                "with no assignment to create or delete: the first matching event starts one task "
                "(the brief given here, then the event's ids and payload), then the watch ends. The "
                "agent receives the events of objects it created, owns, holds or is the parent of. "
                "A deadline is required, at most 7 days ahead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "The agent whose background task the event starts"},
                    "task": {"type": "string", "description": "The task's brief: what to do when the event comes"},
                    "event": {
                        "type": ["string", "array"],
                        "items": {"type": "string"},
                        "description": "A platform event name or a list of them (list_platform_events)",
                    },
                    "expires_at": {
                        "type": "string",
                        "description": "Deadline, ISO 8601 with its offset, e.g. 2026-11-02T09:00:00Z",
                    },
                    "filter": {
                        "type": "object",
                        "description": 'Optional payload filter, e.g. {"correlation_key": "dev:x"} (dotted paths)',
                    },
                    "label": {"type": "string", "description": "Optional short label", "default": ""},
                },
                "required": ["agent_id", "task", "event", "expires_at"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_platform_event_watches",
            description=(
                "List your platform event watches that wake a background task, with their state "
                "(armed, fired, cancelled, expired) and the task a fired one started."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "include_ended": {
                        "type": "boolean",
                        "description": "Also list fired, cancelled and expired watches",
                        "default": False,
                    },
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="cancel_platform_event_watch",
            description="Cancel one of your armed platform event watches before it fires.",
            input_schema={
                "type": "object",
                "properties": {
                    "watch_id": {"type": "string", "description": "The watch id (list_platform_event_watches)"},
                },
                "required": ["watch_id"],
            },
            source="builtin:platform",
        ),
        # ── Integrations (catalog / get / create / update / delete) ───
        ToolDefinition(
            name="list_integration_catalog",
            description=(
                "List integration providers available to connect (e.g. github, "
                "slack, notion). Returns each provider's key, auth type, and the "
                "credential fields it needs. Use before create_integration."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {
                        "type": "string",
                        "description": "Optional case-insensitive filter",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_integration",
            description=(
                "Get details of a specific integration (name, provider, auth "
                "type, connection status). Secrets are never returned."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "integration_id": {
                        "type": "string",
                        "description": (
                            "The id of the integration, or its first 8+ hex "
                            "characters"
                        ),
                    },
                },
                "required": ["integration_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="create_integration",
            description=(
                "Create an integration (external service connection). Provide the "
                "provider key (from list_integration_catalog) and credentials. "
                "API-key/bot-token providers connect immediately; OAuth providers "
                "must still be authorized from the Integrations UI."
                + _SHARE_CONNECTED_ACCOUNT_RULE
                + _spec_tools.CREATE_HINT
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "Provider key (e.g. 'github', 'slack')",
                    },
                    "name": {"type": "string", "description": "Display name"},
                    "auth_type": {
                        "type": "string",
                        "description": (
                            "Auth type (default: the provider's): 'api_key', "
                            "'bot_token', 'oauth', 'none' or 'platform_managed'"
                        ),
                        "default": "",
                    },
                    "api_key": {
                        "type": "string",
                        "description": "API key or bot token (stored encrypted)",
                        "default": "",
                    },
                    "client_id": {
                        "type": "string",
                        "description": "OAuth client ID (stored encrypted)",
                        "default": "",
                    },
                    "client_secret": {
                        "type": "string",
                        "description": "OAuth client secret (stored encrypted)",
                        "default": "",
                    },
                    "extra_config": {
                        "type": "string",
                        "description": (
                            "JSON object string for provider-specific config "
                            '(e.g. \'{"base_url": "https://..."}\')'
                        ),
                        "default": "",
                    },
                    "description": {"type": "string", "default": ""},
                    "is_company_managed": {
                        "type": "boolean",
                        "description": "Share with all users (admin only)",
                        "default": False,
                    },
                    _spec_tools.SPEC_ARGUMENT: _spec_tools.SPEC_ARGUMENT_SCHEMA,
                    "tool_slug": _TOOL_SLUG_SCHEMA,
                    "share_connected_account": _SHARE_CONNECTED_ACCOUNT_SCHEMA,
                    "credential_mode": _CREDENTIAL_MODE_SCHEMA,
                    "a2a_direction": _A2A_DIRECTION_SCHEMA,
                },
                "required": ["provider", "name"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="update_integration",
            description=(
                "Update an integration (name, description, tool suffix, tags, "
                "credentials, auth type, OAuth endpoint fields, group sharing, "
                "company-managed); unknown fields are refused. Switching auth_type to/from "
                "'platform_managed' resets credentials and connection status. "
                "Group members can use it or also manage it; an agent shared "
                "with a group only uses the integrations each member may use."
                + _SHARE_CONNECTED_ACCOUNT_RULE
                + " Creator, config-tier "
                "member or admin only."
                + _spec_tools.UPDATE_HINT
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "integration_id": {
                        "type": "string",
                        "description": "The UUID of the integration to update",
                    },
                    "name": {"type": "string", "default": ""},
                    "description": {"type": "string", "default": ""},
                    "auth_type": {
                        "type": "string",
                        "description": "api_key, bot_token, oauth, none or platform_managed",
                        "default": "",
                    },
                    "api_key": {"type": "string", "default": ""},
                    "client_id": {"type": "string", "default": ""},
                    "client_secret": {"type": "string", "default": ""},
                    "scopes": {
                        "type": "string",
                        "description": "OAuth scopes",
                        "default": "",
                    },
                    "authorize_url": {
                        "type": "string",
                        "description": "OAuth authorization endpoint URL",
                        "default": "",
                    },
                    "token_url": {
                        "type": "string",
                        "description": "OAuth token endpoint URL",
                        "default": "",
                    },
                    "extra_config": {
                        "type": "string",
                        # Merged key by key into the stored config (#9898): a key
                        # left out is kept; the answer reads the stored config back.
                        "description": "JSON keys to change; null removes one",
                        "default": "",
                    },
                    "tags": {"type": "string", "default": ""},
                    "shared_group_ids": {
                        "type": "string",
                        "description": "Comma-separated group UUIDs (REPLACES the set); 'none' unshares",
                        "default": "",
                    },
                    "shared_group_permissions": {
                        "type": "string",
                        "description": (
                            "JSON {group_id: 'use'|'manage'} or 'group_id:use' pairs: 'use' "
                            "(default) binds and runs, 'manage' also edits; keys in shared_group_ids"
                        ),
                        "default": "",
                    },
                    _spec_tools.SPEC_ARGUMENT: _spec_tools.SPEC_ARGUMENT_SCHEMA,
                    "tool_slug": _TOOL_SLUG_SCHEMA,
                    "share_connected_account": _SHARE_CONNECTED_ACCOUNT_SCHEMA,
                    "credential_mode": _CREDENTIAL_MODE_SCHEMA,
                    "a2a_direction": _A2A_DIRECTION_SCHEMA,
                    "is_company_managed": {"type": "boolean", "description": "Admin only"},
                },
                "required": ["integration_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="delete_integration",
            description=(
                "Delete an integration. Creator or admin only. This action cannot "
                "be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "integration_id": {
                        "type": "string",
                        "description": "The UUID of the integration to delete",
                    },
                },
                "required": ["integration_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="import_agent_memory",
            description=(
                "Recreate persistent memory entries on an agent you can manage, "
                "for a migration or a restore: each entry keeps its created_at, "
                "memory_type, subject_key, source and user scope, and a state "
                "entry supersedes the earlier one on its subject. At most 200 "
                "entries per call. Entries scoped to another user need the "
                "agent's owner (or an admin on a company-managed agent)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The UUID of the agent to import into",
                    },
                    "entries": {
                        "type": "array",
                        "maxItems": 200,
                        "description": "The entries, as GET /agents/{id}/memory lists them",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "memory_type": {
                                    "type": "string",
                                    "description": "fact (default), style, preference, state, learned...",
                                },
                                "subject_key": {"type": "string"},
                                "source": {"type": "string"},
                                "user_id": {
                                    "type": "string",
                                    "description": "The user the entry is scoped to; omit for agent-level",
                                },
                                "created_at": {
                                    "type": "string",
                                    "description": "ISO-8601 date the entry was first stored",
                                },
                            },
                            "required": ["content"],
                        },
                    },
                },
                "required": ["agent_id", "entries"],
            },
            source="builtin:platform",
        ),
        # ── Prompts ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_prompts",
            description=(
                "List prompts visible to the user (attachable to agents)."
            ),
            input_schema=_catalog_list_schema(),
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_prompt",
            description=(
                "Get a prompt's details, including its full content "
                "(never truncated) so it can be faithfully edited."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt_id": {
                        "type": "string",
                        "description": "The UUID of the prompt",
                    },
                },
                "required": ["prompt_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="create_prompt",
            description=(
                "Create a reusable prompt template. Attach it to an agent later "
                "with update_agent (prompt_ids) so its content is injected into "
                "the agent's system prompt."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Prompt name"},
                    "content": {
                        "type": "string",
                        "description": "The prompt text / markdown",
                    },
                    "description": {"type": "string", "default": ""},
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags",
                        "default": "",
                    },
                    "hide_from_chat": {
                        "type": "boolean",
                        "description": "Hide from the chat prompt picker",
                        "default": False,
                    },
                    "is_company_managed": {
                        "type": "boolean",
                        "description": "Share with all users (admin only)",
                        "default": False,
                    },
                },
                "required": ["name", "content"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="update_prompt",
            description=(
                "Update a prompt; only the fields given change. 'mode' as "
                "update_skill. A content edit writes a version. Creator or admin only."
                + _CATALOG_ACCESS_DESCRIPTION
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt_id": {
                        "type": "string",
                        "description": "The UUID of the prompt to update",
                    },
                    "name": {"type": "string", "default": ""},
                    "content": {"type": "string", "default": ""},
                    "description": {"type": "string", "default": ""},
                    "tags": {"type": "string", "default": ""},
                    "hide_from_chat": {
                        "type": "string",
                        "description": "'true' or 'false'",
                        "default": "",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["replace", "replace_section", "insert_after", "append", "edit"],
                    },
                    "section": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string"},
                                "replace": {"type": "string"},
                                "replace_all": {"type": "boolean"},
                            },
                            "required": ["find", "replace"],
                        },
                    },
                    "confirm_shrink": {"type": "boolean"},
                    **_CATALOG_ACCESS_PROPERTIES,
                },
                "required": ["prompt_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="delete_prompt",
            description=(
                "Delete a prompt. Creator or admin only. This action cannot be "
                "undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt_id": {
                        "type": "string",
                        "description": "The UUID of the prompt to delete",
                    },
                },
                "required": ["prompt_id"],
            },
            source="builtin:platform",
        ),
        # ── Skills ────────────────────────────────────────────────────
        ToolDefinition(
            name="list_skills",
            description=(
                "List skills visible to the user (instruction sets for agents)."
            ),
            input_schema=_catalog_list_schema(),
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_skill",
            description=(
                "Get a skill's details, including its full content "
                "(never truncated) so it can be faithfully edited. For a long "
                "skill, 'outline': true lists its headings and 'section' reads "
                "one section verbatim instead of the whole body; update_skill "
                "then changes that section alone (mode 'replace_section')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "The UUID of the skill",
                    },
                    "outline": {
                        "type": "boolean",
                        "description": "true: the heading outline instead of the content.",
                        "default": False,
                    },
                    "section": {
                        "type": "string",
                        "description": (
                            "ONE section verbatim: heading text, path "
                            "'Parent > Child' or outline index '#3'."
                        ),
                        "default": "",
                    },
                },
                "required": ["skill_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="create_skill",
            description=(
                "Create a reusable skill. Provide either a 'url' to a SKILL.md "
                "file (fetched and cached) OR 'content' (markdown pasted "
                "directly). YAML frontmatter (name/description/compatibility) is "
                "parsed automatically. Attach it to an agent with update_agent "
                "(skill_ids)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Skill name (optional — derived from frontmatter / "
                            "URL when omitted)"
                        ),
                        "default": "",
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "URL of a SKILL.md (this or 'content', exactly one)."
                        ),
                        "default": "",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Inline Markdown (this or 'url', exactly one)."
                        ),
                        "default": "",
                    },
                    "description": {"type": "string", "default": ""},
                    "compatibility": {
                        "type": "string",
                        "description": "Environment requirements note",
                        "default": "",
                    },
                    "tags": {"type": "string", "default": ""},
                    "hide_from_chat": {
                        "type": "boolean",
                        "default": False,
                    },
                    "is_company_managed": {
                        "type": "boolean",
                        "description": "Share with all users (admin only)",
                        "default": False,
                    },
                },
                "required": [],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="update_skill",
            description=(
                "Update a skill; only the fields given change. 'content' replaces "
                "the body and detaches any URL; 'url' re-fetches a SKILL.md (not "
                "both). Change part of a long skill by mode, never resend it whole. "
                "Every content change writes a skill version. Creator or admin only."
                + _CATALOG_ACCESS_DESCRIPTION
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "Skill UUID",
                    },
                    "name": {"type": "string", "default": ""},
                    "content": {"type": "string", "default": ""},
                    "url": {
                        "type": "string",
                        "description": "SKILL.md URL to re-fetch (not with 'content')",
                        "default": "",
                    },
                    "description": {"type": "string", "default": ""},
                    "compatibility": {"type": "string", "default": ""},
                    "tags": {"type": "string", "default": ""},
                    "enabled": {
                        "type": "string",
                        "description": "'true' or 'false'",
                        "default": "",
                    },
                    "hide_from_chat": {
                        "type": "string",
                        "description": "'true' or 'false'",
                        "default": "",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["replace", "replace_section", "insert_after", "append", "edit"],
                        "description": (
                            "'replace' (default) the body; 'replace_section' the "
                            "'section' alone; 'insert_after' a new section after it; "
                            "'append' at the end; 'edit' applies 'edits'."
                        ),
                        "default": "replace",
                    },
                    "section": {
                        "type": "string",
                        "description": (
                            "Heading text, 'Parent > Child' or '#3'; 'content' is "
                            "then the whole section, heading included."
                        ),
                        "default": "",
                    },
                    "edits": {
                        "type": "array",
                        "description": (
                            "Mode 'edit': exact find/replace, each find unique "
                            "(or replace_all); all or nothing."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string"},
                                "replace": {"type": "string"},
                                "replace_all": {"type": "boolean"},
                            },
                            "required": ["find", "replace"],
                        },
                    },
                    "confirm_shrink": {
                        "type": "boolean",
                        "description": (
                            "true lets 'replace' drop over half of a skill "
                            "of 2,000+ characters."
                        ),
                        "default": False,
                    },
                    **_CATALOG_ACCESS_PROPERTIES,
                },
                "required": ["skill_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="delete_skill",
            description=(
                "Delete a skill. Creator or admin only. This action cannot be "
                "undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "The UUID of the skill to delete",
                    },
                },
                "required": ["skill_id"],
            },
            source="builtin:platform",
        ),
        # ── Variables ─────────────────────────────────────────────────
        ToolDefinition(
            name="list_variables",
            description=(
                "List template variables visible to the user. Variables can be "
                "referenced in personas, prompts, skills, and tool definitions "
                "using the `{{var_id.VARIABLE_ID}}` syntax. Secret values are masked."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_variable",
            description=(
                "Get a variable's details. Secret values are masked and never "
                "returned in plaintext."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "variable_id": {
                        "type": "string",
                        "description": "The UUID of the variable",
                    },
                },
                "required": ["variable_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="create_variable",
            description=(
                "Create a template variable for reuse across prompts/personas/"
                "tools. Reference it as `{{var_id.VARIABLE_ID}}`. Mark is_secret=true to "
                "store the value encrypted (it will never be echoed back)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Variable name"},
                    "value": {
                        "type": "string",
                        "description": "The variable value",
                        "default": "",
                    },
                    "is_secret": {
                        "type": "boolean",
                        "description": "Store the value encrypted and masked",
                        "default": False,
                    },
                    "description": {"type": "string", "default": ""},
                    "tags": {"type": "string", "default": ""},
                    "is_company_managed": {
                        "type": "boolean",
                        "description": "Share with all users (admin only)",
                        "default": False,
                    },
                },
                "required": ["name"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="update_variable",
            description=(
                "Update a variable. Provide only the fields to change. Creator or "
                "admin only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "variable_id": {
                        "type": "string",
                        "description": "The UUID of the variable to update",
                    },
                    "name": {"type": "string", "default": ""},
                    "value": {"type": "string", "default": ""},
                    "is_secret": {
                        "type": "string",
                        "description": "'true' or 'false'",
                        "default": "",
                    },
                    "description": {"type": "string", "default": ""},
                    "tags": {"type": "string", "default": ""},
                },
                "required": ["variable_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="delete_variable",
            description=(
                "Delete a variable. Creator or admin only. This action cannot be "
                "undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "variable_id": {
                        "type": "string",
                        "description": "The UUID of the variable to delete",
                    },
                },
                "required": ["variable_id"],
            },
            source="builtin:platform",
        ),
        # ── MCP Servers ───────────────────────────────────────────────
        ToolDefinition(
            name="list_mcp_servers",
            description=(
                "List MCP servers visible to the user. Returns name, ID, "
                "transport, enabled status, and discovered tool count."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_mcp_server",
            description=(
                "Get an MCP server's details, including its discovered tools."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "The UUID of the MCP server",
                    },
                },
                "required": ["server_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="create_mcp_server",
            description=(
                "Create an MCP (Model Context Protocol) server connection. For "
                "'stdio' provide command + args; for 'sse'/'streamable_http' "
                "provide a url (+ optional headers). Secrets in env/headers are "
                "stored encrypted. Attach it to an agent with update_agent "
                "(mcp_server_ids)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Server name"},
                    "transport": {
                        "type": "string",
                        "description": (
                            "'stdio' (default), 'sse', or 'streamable_http'"
                        ),
                        "default": "stdio",
                    },
                    "command": {
                        "type": "string",
                        "description": "Executable for stdio transport",
                        "default": "",
                    },
                    "args": {
                        "type": "string",
                        "description": (
                            "Arguments — a JSON array string or comma-separated"
                        ),
                        "default": "",
                    },
                    "url": {
                        "type": "string",
                        "description": "Endpoint URL for sse/streamable_http",
                        "default": "",
                    },
                    "env": {
                        "type": "string",
                        "description": (
                            "JSON object string of env vars (stored encrypted)"
                        ),
                        "default": "",
                    },
                    "headers": {
                        "type": "string",
                        "description": (
                            "JSON object string of HTTP headers (stored encrypted)"
                        ),
                        "default": "",
                    },
                    "is_company_managed": {
                        "type": "boolean",
                        "description": "Share with all users (admin only)",
                        "default": False,
                    },
                },
                "required": ["name"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="update_mcp_server",
            description=(
                "Update an MCP server. Provide only the fields to change. "
                "Supports the transport, stdio (command/args), remote "
                "(url/headers), OAuth (auth_mode/client_id/client_secret/scopes/"
                "authorize_url/token_url), tags, hide_from_chat, and enabled "
                "fields. Secrets (env, headers, client_id, client_secret) are "
                "stored encrypted and never echoed back. Creator or admin only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "The UUID of the MCP server to update",
                    },
                    "name": {"type": "string", "default": ""},
                    "transport": {
                        "type": "string",
                        "description": "'stdio', 'sse', or 'streamable_http'",
                        "default": "",
                    },
                    "command": {"type": "string", "default": ""},
                    "args": {
                        "type": "string",
                        "description": "JSON array string or comma-separated",
                        "default": "",
                    },
                    "url": {"type": "string", "default": ""},
                    "env": {
                        "type": "string",
                        "description": "JSON object string (stored encrypted)",
                        "default": "",
                    },
                    "headers": {
                        "type": "string",
                        "description": "JSON object string (stored encrypted)",
                        "default": "",
                    },
                    "auth_mode": {
                        "type": "string",
                        "description": (
                            "'headers' (static headers) or 'oauth'. Switching "
                            "to 'headers' clears any existing OAuth session."
                        ),
                        "default": "",
                    },
                    "client_id": {
                        "type": "string",
                        "description": "OAuth client ID (stored encrypted)",
                        "default": "",
                    },
                    "client_secret": {
                        "type": "string",
                        "description": "OAuth client secret (stored encrypted)",
                        "default": "",
                    },
                    "scopes": {
                        "type": "string",
                        "description": "OAuth scopes (space- or comma-separated)",
                        "default": "",
                    },
                    "authorize_url": {
                        "type": "string",
                        "description": "OAuth authorization endpoint URL",
                        "default": "",
                    },
                    "token_url": {
                        "type": "string",
                        "description": "OAuth token endpoint URL",
                        "default": "",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags",
                        "default": "",
                    },
                    "hide_from_chat": {
                        "type": "string",
                        "description": "'true' or 'false'",
                        "default": "",
                    },
                    "enabled": {
                        "type": "string",
                        "description": "'true' or 'false'",
                        "default": "",
                    },
                },
                "required": ["server_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="delete_mcp_server",
            description=(
                "Delete an MCP server and unbind it from all agents. Creator or "
                "admin only. This action cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "server_id": {
                        "type": "string",
                        "description": "The UUID of the MCP server to delete",
                    },
                },
                "required": ["server_id"],
            },
            source="builtin:platform",
        ),
        # ── Knowledge Bases (get / update / delete + documents) ───────
        ToolDefinition(
            name="get_knowledge_base",
            description=(
                "Get a knowledge base's details (document count, chunk count, "
                "tags, visibility)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "knowledge_base_id": {
                        "type": "string",
                        "description": "The UUID of the knowledge base",
                    },
                },
                "required": ["knowledge_base_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="update_knowledge_base",
            description=(
                "Update a knowledge base's name, description, tags, or "
                "company-managed (platform-wide visibility) status. Provide only "
                "the fields to change. Creator or admin only; changing the "
                "company-managed status is admin only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "knowledge_base_id": {
                        "type": "string",
                        "description": "The UUID of the knowledge base to update",
                    },
                    "name": {"type": "string", "default": ""},
                    "description": {"type": "string", "default": ""},
                    "tags": {"type": "string", "default": ""},
                    "is_company_managed": {
                        "type": "string",
                        "description": (
                            "Set to 'true' or 'false' to change the knowledge "
                            "base's company-managed (platform-wide visibility) "
                            "status. Admin only. 'true' shares it with all users "
                            "(read-only for non-admins); 'false' makes it private "
                            "again and removes it from other users' agents."
                        ),
                        "default": "",
                    },
                },
                "required": ["knowledge_base_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="delete_knowledge_base",
            description=(
                "Delete a knowledge base, all its documents, and embeddings, and "
                "unbind it from agents/assignments. Creator or admin only. This "
                "action cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "knowledge_base_id": {
                        "type": "string",
                        "description": "The UUID of the knowledge base to delete",
                    },
                },
                "required": ["knowledge_base_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="list_kb_documents",
            description=(
                "List the documents in a knowledge base (most recent 100), with "
                "their IDs, source types, and processing status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "knowledge_base_id": {
                        "type": "string",
                        "description": "The UUID of the knowledge base",
                    },
                },
                "required": ["knowledge_base_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="get_kb_document",
            description=(
                "Fetch a SINGLE knowledge-base document by its exact ID and "
                "return its FULL, de-chunked content plus processing status. "
                "This is a deterministic exact-match get (NOT semantic) — use "
                "it to read back a specific document by id, e.g. when using a "
                "knowledge base as a case-file / work-queue store (pairs with "
                "list_kb_documents to enumerate and delete_kb_document to "
                "consume). Unlike search_knowledge (which returns similar "
                "fragments ranked by relevance), this returns the whole "
                "document reassembled, and reports the embedding processing "
                "status so you can detect a not-yet-indexed document "
                "(write-then-read)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "knowledge_base_id": {
                        "type": "string",
                        "description": "The UUID of the knowledge base",
                    },
                    "document_id": {
                        "type": "string",
                        "description": (
                            "The UUID of the document to fetch "
                            "(e.g. from list_kb_documents)"
                        ),
                    },
                },
                "required": ["knowledge_base_id", "document_id"],
            },
            source="builtin:platform",
        ),
        ToolDefinition(
            name="delete_kb_document",
            description=(
                "Delete a single document from a knowledge base. Creator or admin "
                "only. This action cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "knowledge_base_id": {
                        "type": "string",
                        "description": "The UUID of the knowledge base",
                    },
                    "document_id": {
                        "type": "string",
                        "description": "The UUID of the document to delete",
                    },
                },
                "required": ["knowledge_base_id", "document_id"],
            },
            source="builtin:platform",
        ),
    ]

    # create_agent takes every field update_agent takes: its schema
    # gains each update_agent property it does not declare itself, so the two
    # can never drift.
    _positions = {d.name: index for index, d in enumerate(definitions)}
    _create_definition = definitions[_positions["create_agent"]]
    _create_properties = dict(_create_definition.input_schema["properties"])
    for _field, _schema in definitions[_positions["update_agent"]].input_schema["properties"].items():
        if _field not in _create_properties and _field not in UPDATE_AGENT_ONLY_FIELDS:
            _create_properties[_field] = _schema
    definitions[_positions["create_agent"]] = dataclasses.replace(
        _create_definition,
        input_schema={**_create_definition.input_schema, "properties": _create_properties},
    )

    # ── Workspaces: their own module, the same executor ───────
    from .workspaces import build_workspace_tools

    workspace_defs, workspace_handlers = build_workspace_tools(
        user_id, conversation_id=conversation_id
    )
    for tool_name, handler in workspace_handlers.items():
        executor.register(tool_name, handler)
    definitions.extend(workspace_defs)

    # ── Agent accounts: their own module, the same executor ───
    from .agent_accounts import build_agent_account_tools

    account_defs, account_handlers = build_agent_account_tools(user_id)
    for tool_name, handler in account_handlers.items():
        executor.register(tool_name, handler)
    definitions.extend(account_defs)

    # ── Intent bindings: their own module, the same executor ───
    from .intent_bindings import build_intent_binding_tools

    intent_defs, intent_handlers = build_intent_binding_tools(user_id)
    for tool_name, handler in intent_handlers.items():
        executor.register(tool_name, handler)
    definitions.extend(intent_defs)

    # ── Knowledge base sync sources: their own module, the same executor ───
    from .kb_sync_sources import build_kb_sync_source_tools

    sync_source_defs, sync_source_handlers = build_kb_sync_source_tools(user_id)
    for tool_name, handler in sync_source_handlers.items():
        executor.register(tool_name, handler)
    definitions.extend(sync_source_defs)

    # A Platform Health run reads the platform here, never changes it (#8255).
    from app.services.health.sandbox.guards import guard_agent_builder

    return guard_agent_builder(definitions, executor), executor
