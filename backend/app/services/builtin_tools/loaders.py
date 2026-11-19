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

"""MCP tool loading, custom tool loading, and MCP manager cleanup.

Moved verbatim from the original ``app/services/builtin_tools.py`` module
as part of the package split (mechanical refactor - no behavior change).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from base64 import b64decode
from typing import TYPE_CHECKING

from app.core.utils import format_exception_message, safe_uuid
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import _persist_generated_file, logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    # ``ToolResult`` is imported here (not at module scope) so we can
    # reference it in the deferred (PEP 563 /
    # ``from __future__ import annotations``) return annotation on
    # ``_run_custom`` below — that closure returns either a plain
    # ``str`` (legacy fast path) or a ``ToolResult`` (when the custom
    # tool emits ``$output_files``). The actual runtime ``ToolResult``
    # symbol is still imported lazily inside the function body to keep
    # the import graph for the common fast path unchanged.
    from app.tools.base import ToolResult

# ── MCP tool loading ─────────────────────────────────────────────────────────

# Separator used by MCP servers to create namespaced tool variants.
# e.g. "search_deals___filigran__hubspot__read-write" is a namespaced
# variant of the base tool "search_deals".
_MCP_TOOL_NAMESPACE_SEPARATOR = "___"


def _deduplicate_namespaced_tools(tools: list[ToolDefinition]) -> list[ToolDefinition]:
    """Remove namespaced MCP tool variants that duplicate a base tool.

    Many MCP servers expose both a short base name (e.g. ``search_deals``)
    and a fully-qualified namespaced variant (e.g.
    ``search_deals___filigran__hubspot__read-write``).  When both are
    presented to the LLM, it may route natural-language queries to the
    longer variant inconsistently, leading to non-compliance with agent
    instructions about which parameters to include.

    This function keeps only the base tool when a namespaced variant
    exists whose prefix (before ``___``) matches another tool's full name
    from the same batch.
    """
    if not tools:
        return tools

    # Collect base names (tools without the namespace separator)
    base_names: set[str] = set()
    for t in tools:
        name = t.name
        if _MCP_TOOL_NAMESPACE_SEPARATOR not in name:
            base_names.add(name)

    if not base_names:
        return tools

    # Filter out namespaced variants whose prefix matches a base tool
    deduped: list[ToolDefinition] = []
    for t in tools:
        name = t.name
        if _MCP_TOOL_NAMESPACE_SEPARATOR in name:
            prefix = name.split(_MCP_TOOL_NAMESPACE_SEPARATOR, 1)[0]
            if prefix in base_names:
                logger.info(
                    "Deduplicating namespaced MCP tool '%s' (base tool '%s' already present)",
                    name,
                    prefix,
                )
                continue
        deduped.append(t)

    return deduped


def format_mcp_auth_broken_prompt(broken_servers: list[str]) -> str:
    """Build the Tool Availability Notice for MCP servers with broken credentials.

    Returned when the cross-run auth-failure counter (Redis) for one or more
    servers has exceeded the escalation threshold, meaning the credential is
    persistently invalid or expired.  The LLM is warned upfront so it
    acknowledges the limitation instead of wasting iterations calling tools
    that are known to fail.

    Returns an empty string when *broken_servers* is empty (net-zero prompt
    inflation on healthy runs).  Shared by ``assignment_engine``,
    ``agent_chat``, and ``general_chat`` (insert the result before the
    security anchor via ``prompt_guard.insert_before_safety_anchor``).
    """
    if not broken_servers:
        return ""
    # json.dumps-escape each name: server names are user-controlled DB
    # values, so quotes / newlines / control characters must not distort
    # the notice or smuggle extra prompt lines.
    names = ", ".join(json.dumps(n) for n in broken_servers)
    return (
        f"\n\n**Tool Availability Notice**: The following MCP "
        f"server(s) have a persistently broken credential (the API "
        f"token or key is likely expired, revoked, or missing) and "
        f"their tools will fail: {names}. Do not attempt to call "
        f"these tools. If the task requires them, acknowledge this "
        f"limitation clearly in your response."
    )


def format_mcp_unavailable_prompt(
    failed_servers: list[str],
    *,
    all_failed: bool = False,
    server_durations: dict[str, int] | None = None,
) -> str:
    """Build the Tool Availability Notice for unreachable MCP servers.

    Returns an empty string when *failed_servers* is empty (and *all_failed*
    is False), so the block is injected ONLY on runs where servers actually
    failed to connect — net-zero prompt inflation on healthy runs.  Shared by
    ``assignment_engine``, ``agent_chat``, and ``general_chat`` (insert the
    result before the security anchor via
    ``prompt_guard.insert_before_safety_anchor``).

    *all_failed* covers the wrapper-level failure path of
    :func:`load_mcp_tools` (DB error, no enabled servers, import error —
    the manager is ``None`` so no per-server names are available): a generic
    notice is emitted instead of naming individual servers.

    *server_durations* is an optional mapping of server name → whole hours
    since first failure.  When a server has been unreachable for ≥ 24 hours
    the notice includes a human-readable duration (e.g. "unreachable for 3
    days") so the LLM can surface a more informative limitation message.
    """
    if all_failed:
        # Context-neutral wording: this helper serves agent chat, the
        # General Assistant (not an agent), and assignment runs alike.
        return (
            "\n\n**Tool Availability Notice**: The configured MCP tool "
            "servers could not be loaded and their tools are unavailable "
            "for this run. If the task requires those tools, acknowledge "
            "this limitation clearly in your response."
        )
    if not failed_servers:
        return ""
    # Deferred import: ``app.tools.mcp_client`` pulls in the MCP SDK
    # transports, and this module deliberately keeps its module-scope
    # import graph light (see the TYPE_CHECKING note above).
    from app.tools.mcp_client import format_failure_duration

    # Build per-server fragments with optional duration annotation.
    # json.dumps-escape each name: server names are user-controlled DB
    # values, so quotes / newlines / control characters must not be able
    # to distort the notice or smuggle extra prompt lines.
    parts: list[str] = []
    for n in failed_servers:
        escaped = json.dumps(n)
        hours = (server_durations or {}).get(n)
        if hours is not None and hours >= 24:
            duration_str = format_failure_duration(hours)
            parts.append(f"{escaped} (unreachable for {duration_str})")
        else:
            parts.append(escaped)
    names = ", ".join(parts)
    return (
        f"\n\n**Tool Availability Notice**: The following MCP "
        f"server(s) are currently unreachable and their tools are "
        f"unavailable for this run: {names}. If the task "
        f"requires those tools, acknowledge this limitation clearly "
        f"in your response."
    )


async def load_mcp_tools(
    db: AsyncSession,
    mcp_ids: list,
    composite: object,
    allowed_tools: dict | None = None,
    artifact_scope: object | None = None,
    acting_user_id: object | None = None,
) -> object | None:
    """Load MCP server tools into a :class:`CompositeToolExecutor`.

    Queries the database for enabled MCP servers matching *mcp_ids*, creates
    an :class:`MCPManager`, registers tools (using cached tools when available
    for fast startup, with lazy connect on first call), and applies optional
    per-server tool scoping from *allowed_tools*.

    *artifact_scope* (an :class:`~app.tools.mcp_client.MCPArtifactScope`) is
    what a binary tool result is persisted under - pass the scope the run's
    ``generate_file`` uses.

    *acting_user_id* is the person the calls are made for: on a ``per_user``
    OAuth server they carry that person's own token (none without one,
    never another person's).

    Returns the :class:`MCPManager` instance (caller **must** call
    ``await cleanup_mcp_manager(manager)`` when done) or *None* if no MCP
    servers could be loaded.
    """
    if not mcp_ids:
        return None
    try:
        from sqlalchemy import select

        from app.api.v1.helpers import build_mcp_runtime
        from app.models.mcp_server import MCPServer
        from app.services.vault import SecretUnavailableError
        from app.tools.mcp_client import MCPManager, get_mcp_failure_hours

        mcp_uuids: list[uuid.UUID] = []
        not_server_ids: list[str] = []
        for mid in mcp_ids:
            parsed = mid if isinstance(mid, uuid.UUID) else safe_uuid(str(mid))
            if parsed is None:
                not_server_ids.append(str(mid))
            else:
                mcp_uuids.append(parsed)
        if not_server_ids:
            # An entry that names no server (a bundle-local reference stored by
            # an older import, a redacted placeholder) is skipped on its own:
            # failing the load would drop every other server of the list too.
            logger.warning(
                "Ignoring %d MCP server reference(s) that are not server ids: %r",
                len(not_server_ids),
                not_server_ids[:5],
            )
        if not mcp_uuids:
            return None
        result = await db.execute(
            select(MCPServer).where(
                MCPServer.id.in_(mcp_uuids),
                MCPServer.enabled.is_(True),
            )
        )
        mcp_servers = {str(srv.id): srv for srv in result.scalars().all()}
        if not mcp_servers:
            return None

        from app.services.mcp_user_credentials import is_per_user, user_credentials_by_server

        # The acting person's own accounts on the per_user servers, one query.
        own_credentials = await user_credentials_by_server(
            db,
            [srv.id for srv in mcp_servers.values() if is_per_user(srv)],
            acting_user_id,
        )

        import asyncio as _aio
        import dataclasses as _dc

        mcp_manager = MCPManager(artifact_scope=artifact_scope)

        # Separate cached (instant) from uncached (needs network connect).
        _uncached: list[tuple[str, object]] = []  # (mcp_id_str, runtime)
        _cached_results: list[tuple[str, list]] = []  # (mcp_id_str, tools)

        for mcp_id in mcp_ids:
            srv = mcp_servers.get(str(mcp_id))
            if not srv:
                continue
            try:
                runtime = await build_mcp_runtime(srv, own_credentials.get(srv.id))
            except SecretUnavailableError:
                # A header or variable stored in a vault could not be read:
                # this server is unavailable, every other one still loads.
                mcp_manager.record_failed_server(srv.name)
                continue
            if srv.discovered_tools:
                tools = mcp_manager.register_cached_server(
                    runtime, srv.discovered_tools
                )
                _cached_results.append((str(mcp_id), tools))
            else:
                _uncached.append((str(mcp_id), runtime))

        # Connect uncached servers concurrently
        if _uncached:
            # Hand the pooled connection back before the network connects:
            # a first-use / cache-cleared server performs a full handshake
            # (stdio spawn or HTTP/SSE connect, seconds or a timeout) and
            # the SELECT above opened a transaction on the caller's session
            # that would pin its connection for the whole gather.
            # Setup phases are read-only here, so the
            # commit is safe; SQLAlchemy re-acquires on the next query.
            from app.core.database import release_db_connection_before_loop

            await release_db_connection_before_loop(db)
            _connect_results = await _aio.gather(
                *(mcp_manager.connect_server(rt) for _, rt in _uncached),
                return_exceptions=True,
            )
            for (mid_str, rt), result in zip(_uncached, _connect_results, strict=True):
                if isinstance(result, _aio.CancelledError):
                    # A CancelledError does not mean the server is
                    # unreachable (the connect task can be cancelled by an
                    # overall run cancellation/timeout) — recording it would
                    # produce an incorrect "unreachable" notice, and a
                    # WARNING would be misleading noise during cancellations.
                    logger.info("MCP connect cancelled for %s", mid_str)
                    continue
                if isinstance(result, Exception):
                    logger.warning("MCP connect failed for %s: %s", mid_str, result)
                    # Same Redis key the connection's failure tracking uses
                    # (MCPServerRuntime.failure_key) so the duration lookup
                    # addresses the timestamp the connect attempt just wrote.
                    hours = await get_mcp_failure_hours(rt.failure_key)
                    mcp_manager.record_failed_server(rt.name, hours=hours)
                    continue
                _cached_results.append((mid_str, result))

        exposed = False
        for mid_str, tools in _cached_results:
            if allowed_tools is not None:
                allow_list = allowed_tools.get(mid_str)
                if allow_list is not None:
                    tools = [t for t in tools if t.name in allow_list]
            # Deduplicate namespaced tool variants (e.g. tool___ns__server__perm)
            # that duplicate a base tool from the same server, preventing LLM
            # confusion when both are presented as callable tools.
            tools = _deduplicate_namespaced_tools(tools)
            tools = [_dc.replace(t, entity_id=mid_str) for t in tools]
            composite.add_tools(tools, mcp_manager)
            exposed = exposed or bool(tools)
        if exposed:
            # Reads the resource links those tools return.
            composite.add_tools(mcp_manager.resource_tools(), mcp_manager)

        return mcp_manager
    except Exception as e:
        # ALL MCP tools silently disappeared. ERROR-level because this
        # is one of the highest-impact silent failures: the agent has
        # no idea its tools are missing and will hallucinate or refuse.
        # Per-server connect failures are handled (and logged) inside
        # the asyncio.gather above — this branch fires when the wrapper
        # itself fails (DB error, import error, etc.).
        logger.error(
            "Failed to load MCP tools (%d MCP server(s) requested) — "
            "agent will run with NO MCP tools: %s",
            len(mcp_ids) if mcp_ids else 0,
            format_exception_message(e),
            exc_info=True,
        )
        return None


async def load_custom_tools(
    db: AsyncSession,
    custom_tool_ids: list,
    composite: object,
    *,
    variables: dict[str, str] | None = None,
    user_id: str | None = None,
    quota_user_id: str | None = None,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
) -> None:
    """Load user-defined custom tools (JavaScript or Python) into a :class:`CompositeToolExecutor`.

    Queries the database for enabled custom tools matching *custom_tool_ids*,
    creates sandboxed executors for each, and registers them with the composite.
    ``{{var_id.UUID}}`` placeholders in function bodies are resolved when
    *variables* is provided, enabling secret injection (e.g. API keys).

    Files are injected as ``$files`` (a native list/array) into the sandbox via
    :meth:`FileContext.to_sandbox_files`.  The old per-parameter ``format: "file"``
    convention is removed — all files are always available through ``$files``.

    *quota_user_id*: user to charge the combined agentic+sandbox quota
    against, when it differs from *user_id* — e.g. a company-managed
    assignment run has no personal file owner (``user_id=None``, by design)
    but still has a charged/owning user whose quota should gate and record
    this dispatch. Defaults to *user_id* when not given.
    """
    if not custom_tool_ids:
        return
    try:
        from sqlalchemy import select

        from app.models.custom_tool import CustomTool
        from app.services.custom_tool_executor import execute_tool_auto
        from app.services.variable_resolver import (
            extract_variable_ids,
            substitute_variables,
        )

        ct_uuids = [
            uuid.UUID(str(cid)) if not isinstance(cid, uuid.UUID) else cid
            for cid in custom_tool_ids
        ]
        ct_query = await db.execute(
            select(CustomTool).where(
                CustomTool.id.in_(ct_uuids),
                CustomTool.enabled.is_(True),
            )
        )
        all_custom_tools = {str(ct.id): ct for ct in ct_query.scalars().all()}

        ct_executor = CallableToolExecutor()
        ct_defs: list[ToolDefinition] = []

        for ct_id in custom_tool_ids:
            ct = all_custom_tools.get(str(ct_id))
            if ct and ct.js_function:
                _fn_code = substitute_variables(ct.js_function, variables or {})
                _lang = getattr(ct, "language", "javascript") or "javascript"
                _schema = ct.input_schema
                _timeout = getattr(ct, "timeout", None) or 300
                # {{var_id.UUID}} placeholders that survived variable
                # substitution (e.g. the variable does not exist or is not
                # accessible to this user).  Computed once at load time — the
                # substituted code is immutable for the executor's lifetime.
                _unresolved_var_ids = sorted(extract_variable_ids(_fn_code))

                async def _run_custom(
                    _code: str = _fn_code,
                    _language: str = _lang,
                    _name: str = ct.name,
                    _tout: int = _timeout,
                    _executor: CallableToolExecutor = ct_executor,
                    _unresolved: list[str] = _unresolved_var_ids,
                    **kwargs: object,
                ) -> str | ToolResult:
                    # Pop file_context if it leaked into kwargs (defensive)
                    kwargs.pop("file_context", None)

                    # Fail fast on unresolved placeholders: executing the tool
                    # with a literal placeholder as a credential would result in
                    # a 401 or a 300-second timeout, both of which consume
                    # agentic-loop iterations without any chance of success.
                    if _unresolved:
                        masked = ", ".join(
                            f"{{{{var_id.{uid[:4]}...{uid[-4:]}}}}}" for uid in _unresolved
                        )
                        logger.warning(
                            "Custom tool %r has %d unresolved variable placeholder(s) (%s) — "
                            "the referenced variable(s) could not be resolved; refusing "
                            "execution to prevent using a literal placeholder as a credential",
                            _name,
                            len(_unresolved),
                            masked,
                        )
                        return (
                            f"Error: custom tool '{_name}' has {len(_unresolved)} unresolved "
                            f"variable placeholder(s) ({masked}). The referenced platform "
                            f"variable(s) could not be resolved — verify that the variable "
                            f"exists, is accessible to this user, and is correctly referenced "
                            f"in the tool's function code. Fix the variable binding and retry."
                        )

                    # Inject $files from FileContext with pre-resolved content
                    fc = _executor.file_context
                    kwargs["$files"] = (
                        await fc.to_sandbox_files() if fc and not fc.is_empty else []
                    )
                    # Best-effort attribution for per-user sandbox usage
                    # recording (spec 002-sandbox-quota-usage-tracking) — a
                    # malformed user_id must not break tool execution itself,
                    # just skip usage attribution.
                    _usage_user_id = safe_uuid(user_id)
                    _quota_user_id = safe_uuid(quota_user_id)
                    raw_output = await execute_tool_auto(
                        _code,
                        dict(kwargs),
                        language=_language,
                        timeout=_tout,
                        user_id=_usage_user_id,
                        quota_user_id=_quota_user_id,
                        db=db,
                    )

                    parsed_output: object = None
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        parsed_output = json.loads(raw_output)

                    if not isinstance(parsed_output, dict):
                        return raw_output

                    output_files = parsed_output.get("$output_files")
                    if isinstance(output_files, list):
                        from app.llm.base import FileRef
                        from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES, guess_content_type
                        from app.tools.base import ToolResult

                        produced_refs: list[FileRef] = []
                        for idx, item in enumerate(output_files):
                            # Every skip path below emits a single
                            # WARNING with the entry index so custom-tool
                            # authors can immediately see WHICH entry was
                            # rejected and WHY.  Without this, malformed
                            # payloads silently disappeared from the
                            # response and authors had no signal beyond
                            # "my file didn't show up".  Rate-limiting
                            # isn't needed in practice — ``$output_files``
                            # is bounded by the LLM's tool-call output
                            # budget — but the structured per-entry shape
                            # would batch cleanly if it ever became an
                            # issue.
                            if not isinstance(item, dict):
                                logger.warning(
                                    "Skipping $output_files entry %d for "
                                    "tool %s: expected an object, got %s",
                                    idx,
                                    _name,
                                    type(item).__name__,
                                )
                                continue
                            filename = item.get("filename")
                            content = item.get("content")
                            if not isinstance(filename, str) or not filename.strip():
                                logger.warning(
                                    "Skipping $output_files entry %d for "
                                    "tool %s: missing or blank ``filename`` "
                                    "(must be a non-empty string)",
                                    idx,
                                    _name,
                                )
                                continue
                            if not isinstance(content, str):
                                logger.warning(
                                    "Skipping $output_files entry %d for "
                                    "tool %s (filename=%r): ``content`` must "
                                    "be a string (got %s) — use ``encoding`` "
                                    "= 'base64' for binary payloads",
                                    idx,
                                    _name,
                                    filename,
                                    type(content).__name__,
                                )
                                continue
                            content_encoding = item.get("encoding")
                            # Encoding validation MUST mirror
                            # ``build_generate_file_tool._generate_file``
                            # (the agent-facing sibling that lives a few
                            # hundred lines up in this same module).
                            # Without this validation, a typo such as
                            # ``"Base64"`` / ``"BASE64"`` / ``"hex"`` /
                            # ``"binary"`` falls through to the ``else``
                            # branch and the supposed-binary base64 text
                            # is UTF-8-encoded as raw characters, silently
                            # corrupting the persisted file. ``generate_file``
                            # rejects unsupported encodings outright; we
                            # mirror its accepted set here and skip
                            # (with a uniform WARNING, same diagnostic
                            # shape as the sibling skips above) so a
                            # custom-tool author who typos the encoding
                            # immediately sees the failed entry rather
                            # than discovering a corrupt download later.
                            if content_encoding not in (None, "", "utf-8", "text", "base64"):
                                logger.warning(
                                    "Skipping $output_files entry %d for "
                                    "tool %s (filename=%r): unsupported "
                                    "encoding %r (use 'base64' for binary "
                                    "payloads, omit / 'utf-8' / 'text' "
                                    "for text)",
                                    idx,
                                    _name,
                                    filename,
                                    content_encoding,
                                )
                                continue
                            try:
                                if content_encoding == "base64":
                                    data = b64decode(content, validate=True)
                                else:
                                    data = content.encode("utf-8")
                            except Exception:
                                # Malformed payload (invalid base64, decode
                                # error, etc.); skip this entry but log so
                                # the tool author can correlate the failed
                                # entry to its position + filename in the
                                # outgoing ``$output_files`` array (the
                                # other skip warnings in this block already
                                # include both fields — keep all four in
                                # lock-step so a grep on "Skipping
                                # $output_files entry" surfaces the same
                                # diagnostic surface for every reason).
                                logger.warning(
                                    "Skipping $output_files entry %d for "
                                    "tool %s (filename=%r): invalid %s "
                                    "payload",
                                    idx,
                                    _name,
                                    filename,
                                    content_encoding or "utf-8",
                                    exc_info=True,
                                )
                                continue
                            if len(data) > MAX_UPLOAD_SIZE_BYTES:
                                logger.warning(
                                    "Skipping $output_files entry %d for "
                                    "tool %s (filename=%r): payload exceeds "
                                    "%d bytes",
                                    idx,
                                    _name,
                                    filename,
                                    MAX_UPLOAD_SIZE_BYTES,
                                )
                                continue
                            file_att = await _persist_generated_file(
                                db=db,
                                user_id=user_id or "",
                                filename=filename.strip(),
                                raw_bytes=data,
                                content_type=(
                                    item.get("content_type")
                                    if isinstance(item.get("content_type"), str)
                                    else guess_content_type(filename)
                                ),
                                conversation_id=conversation_id,
                                assignment_run_id=assignment_run_id,
                                source="agent_generated",
                            )
                            fref = FileRef.from_attachment(file_att)
                            produced_refs.append(fref)
                            if fc is not None:
                                fc.add(fref)

                        result_payload = parsed_output.get("result", raw_output)
                        if isinstance(result_payload, str):
                            result_text = result_payload
                        else:
                            result_text = json.dumps(result_payload, default=str)
                        return ToolResult(
                            tool_call_id="",
                            content=result_text,
                            files=produced_refs or None,
                        )

                    # Mirror the ``$output_files`` normalisation above so
                    # ``_run_custom`` actually returns ``str | ToolResult``
                    # as its signature claims. ``parsed_output.get("result", ...)``
                    # can be any JSON type (dict / list / int / float / bool
                    # / None / str) — ``CallableToolExecutor`` happens to
                    # JSON-serialise non-strings downstream, but leaving a
                    # ``dict | list | scalar`` return here makes the
                    # annotation misleading and produces inconsistent
                    # behaviour between the two no-files paths (one returns
                    # ``raw_output: str`` at line 4814, this one returns
                    # arbitrary JSON). Normalising here keeps the contract
                    # honest AND makes the tool author's JSON ``result``
                    # field round-trip back to the LLM as JSON text rather
                    # than relying on the executor's implicit serialisation.
                    result_payload = parsed_output.get("result", raw_output)
                    if isinstance(result_payload, str):
                        return result_payload
                    return json.dumps(result_payload, default=str)

                ct_executor.register(ct.name, _run_custom)

                ct_defs.append(
                    ToolDefinition(
                        name=ct.name,
                        description=substitute_variables(
                            ct.description or f"Custom tool: {ct.name}",
                            variables or {},
                        ),
                        input_schema=_schema or {"type": "object", "properties": {}},
                        source=f"custom:{ct.id}",
                        entity_id=str(ct.id),
                    )
                )

        if ct_defs:
            composite.add_tools(ct_defs, ct_executor)
    except Exception as e:
        # ALL custom tools silently disappeared. ERROR-level because the
        # agent will quietly run without the user's hand-crafted
        # JavaScript/Python tools and the user has no way to see why.
        logger.error(
            "Failed to load custom tools (%d custom tool(s) requested) — "
            "agent will run with NO custom tools: %s",
            len(custom_tool_ids) if custom_tool_ids else 0,
            format_exception_message(e),
            exc_info=True,
        )


class _ToolCollector:
    """Records ``add_tools`` calls for deferred, ordered replay.

    Used by :func:`load_custom_and_mcp_tools` so the two loaders can run
    CONCURRENTLY (each on its own DB session) without interleaving their
    registrations on the shared composite — the batches are replayed onto
    the real composite in a deterministic order once both complete, so the
    tool ordering the LLM sees is identical to the legacy sequential path.
    """

    def __init__(self) -> None:
        self.batches: list[tuple[list[ToolDefinition], object]] = []

    def add_tools(self, definitions: list[ToolDefinition], executor: object) -> None:
        self.batches.append((list(definitions), executor))

    def replay(self, composite: object) -> None:
        for definitions, executor in self.batches:
            composite.add_tools(definitions, executor)


async def load_custom_and_mcp_tools(
    db: AsyncSession,
    *,
    custom_tool_ids: list,
    mcp_ids: list,
    composite: object,
    variables: dict[str, str] | None = None,
    user_id: str | None = None,
    quota_user_id: str | None = None,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
    mcp_allowed_tools: dict | None = None,
    mcp_first: bool = False,
) -> object | None:
    """Load custom tools and MCP tools CONCURRENTLY (P3.5).

    Both :func:`load_custom_tools` and :func:`load_mcp_tools` issue
    ``db.execute(...)``, and SQLAlchemy forbids concurrent operations on
    one ``AsyncSession`` — which historically forced the two loads (and
    the MCP network connects for uncached servers, seconds each) to run
    sequentially in the chat setup phase.  This helper keeps the custom
    loader on the caller's session and gives the MCP loader its OWN
    short-lived read session, so the MCP connect handshakes overlap the
    custom-tool DB work.  Registrations are buffered per loader and
    replayed onto *composite* in a deterministic order (*mcp_first*
    matches each caller's legacy ordering), so the tool list the LLM sees
    is unchanged.

    Returns the MCP manager (caller must still ``cleanup_mcp_manager`` it)
    or ``None``.  When only one loader has work, it runs directly — no
    second session, exactly the legacy path.
    """
    import asyncio as _aio

    has_custom = bool(custom_tool_ids)
    has_mcp = bool(mcp_ids)
    if not has_custom and not has_mcp:
        return None
    if not has_mcp:
        await load_custom_tools(
            db,
            custom_tool_ids,
            composite,
            variables=variables,
            user_id=user_id,
            quota_user_id=quota_user_id,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
        )
        return None
    # Deferred import: ``app.tools.mcp_client`` pulls in the MCP SDK transports,
    # which the custom-only path above never needs.
    from app.tools.mcp_client import MCPArtifactScope

    mcp_scope = MCPArtifactScope(
        user_id=user_id,
        conversation_id=conversation_id,
        assignment_run_id=assignment_run_id,
    )
    if not has_custom:
        return await load_mcp_tools(
            db,
            mcp_ids,
            composite,
            allowed_tools=mcp_allowed_tools,
            artifact_scope=mcp_scope,
            acting_user_id=user_id,
        )

    from app.core.database import async_session

    custom_collector = _ToolCollector()
    mcp_collector = _ToolCollector()

    async def _load_custom() -> None:
        await load_custom_tools(
            db,
            custom_tool_ids,
            custom_collector,
            variables=variables,
            user_id=user_id,
            quota_user_id=quota_user_id,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
        )

    async def _load_mcp() -> object | None:
        # Own session: the MCP SELECT + the (potentially slow) uncached
        # connect handshakes must not touch the caller's session while the
        # custom loader is using it.  The session closes as soon as the
        # loader returns — MCP connections never reference it afterwards.
        #
        # The session acquisition itself sits OUTSIDE ``load_mcp_tools``'s
        # internal catch-all, so it must be guarded here: on the legacy
        # (shared-session) path a session problem degraded to "agent runs
        # without MCP tools", and an exception escaping this task would
        # instead propagate through the gather, abandon the still-running
        # custom loader mid-flight on the caller's session, and fail the
        # whole turn.
        try:
            async with async_session() as mcp_db:
                return await load_mcp_tools(
                    mcp_db,
                    mcp_ids,
                    mcp_collector,
                    allowed_tools=mcp_allowed_tools,
                    artifact_scope=mcp_scope,
                    acting_user_id=user_id,
                )
        except Exception:
            logger.error(
                "Failed to load MCP tools on the split session (%d MCP "
                "server(s) requested) — agent will run with NO MCP tools",
                len(mcp_ids),
                exc_info=True,
            )
            return None

    _, mcp_manager = await _aio.gather(_load_custom(), _load_mcp())

    # Deterministic replay: preserve each caller's legacy registration
    # order so the on-the-wire tool list cannot change turn-to-turn.
    first, second = (
        (mcp_collector, custom_collector)
        if mcp_first
        else (custom_collector, mcp_collector)
    )
    first.replay(composite)
    second.replay(composite)
    return mcp_manager


async def cleanup_mcp_manager(mcp_manager: object | None) -> None:
    """Safely disconnect all MCP server connections.

    Call this in a ``finally`` block after using :func:`load_mcp_tools`.
    Suppresses cleanup errors to avoid masking the original exception.
    """
    if mcp_manager is None:
        return
    try:
        await mcp_manager.disconnect_all()
    except Exception as e:
        logger.warning("MCP cleanup error: %s", format_exception_message(e))
