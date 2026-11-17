# SPDX-License-Identifier: Apache-2.0

"""Hard channel boundary for the Slack tools.

The Slack channels an operator selects in an agent's or an assignment's
Scope(s) (``integration_element_bindings["slack:slack_channel"]``) used to be
prompt text only: the Slack tools accepted any conversation the token could
reach. With a channel scope in the run's effective scope
(``services.element_scope``), ``build_tools`` passes the tools through
:func:`apply_channel_scope`, the same shape as the GitHub repository guard
(``providers/github/scope.py``):

* a call whose ``channel`` (or each of ``upload_slack_file``'s ``channels``)
  is not a selected channel is refused before any request, naming the allowed
  channels. A channel may be given by its ID, ``#name`` or ``<#ID|name>``; a
  name is resolved to the selected channel's ID, never looked up in Slack;
* the listings keep only the selected channels at the data level:
  ``list_slack_channels``, the message and file searches (their totals over
  the whole workspace are not shown), and ``list_slack_files`` needs a
  selected channel;
* a file named by its ID is read or deleted only when it is shared in a
  selected channel;
* the tools that reach conversations outside any channel selection are
  withdrawn: direct messages (``list_slack_dms``, ``open_slack_dm``) and
  ``create_slack_channel`` (a new channel is outside the selection by
  definition).

Workspace members and the caller's own status stay available: they are not a
conversation's content. A selection with no usable channel allows nothing.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import re
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

#: Element-binding key of a Slack channel scope (``integration_elements``).
SLACK_CHANNEL_SCOPE_KEY = "slack:slack_channel"

#: Tools that reach conversations outside any channel selection.
DROPPED_WHEN_SCOPED = frozenset({"list_slack_dms", "open_slack_dm", "create_slack_channel"})

#: Tools whose ``channel`` argument names the one conversation they act on.
CHANNEL_ARGUMENT_TOOLS = frozenset(
    {
        "read_slack_messages",
        "read_slack_thread",
        "send_slack_message",
        "reply_slack_thread",
        "add_slack_reaction",
        "get_slack_reactions",
        "set_slack_channel_topic",
        "set_slack_channel_purpose",
        "archive_slack_channel",
        "rename_slack_channel",
        "invite_to_slack_channel",
        "kick_from_slack_channel",
        "list_slack_bookmarks",
        "add_slack_bookmark",
    }
)

#: Tools that search the whole workspace; their results are filtered.
SEARCH_TOOLS = frozenset({"search_slack_messages", "search_slack_files"})

_MENTION_RE = re.compile(r"^<#([A-Za-z0-9]+)(?:\|[^>]*)?>$")

#: The channel scope of the Slack tool call in progress (channel ID -> name),
#: set by the guard for that one call and read by the listings and file tools.
_CHANNEL_SCOPE: ContextVar[dict[str, str] | None] = ContextVar("slack_channel_scope", default=None)


def channel_scope_from_bindings(element_scope: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The selected channels (upper-cased ID -> name), or ``None``.

    *element_scope* is a run's EFFECTIVE scope (``services.element_scope``):
    ``None`` means no channel scope was set. A scope with no usable entry -
    including the empty list a narrowing produces - is an EMPTY mapping that
    allows nothing, never no boundary at all.
    """
    if not isinstance(element_scope, Mapping):
        return None
    items = element_scope.get(SLACK_CHANNEL_SCOPE_KEY)
    if items is None:
        return None
    if not isinstance(items, list):
        return {}
    scope: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        channel_id = item.get("id")
        if not isinstance(channel_id, str) or not channel_id.strip():
            continue
        name = item.get("name")
        scope[channel_id.strip().upper()] = name.strip().lstrip("#") if isinstance(name, str) else ""
    return scope


def active_channel_scope() -> dict[str, str] | None:
    """The channel scope of the call in progress (``None`` = unscoped)."""
    return _CHANNEL_SCOPE.get()


def in_channel_scope(channel_id: Any) -> bool:
    """True when the call in progress may use conversation *channel_id*."""
    scope = _CHANNEL_SCOPE.get()
    if scope is None:
        return True
    return isinstance(channel_id, str) and channel_id.strip().upper() in scope


def allowed_text(scope: Mapping[str, str]) -> str:
    if not scope:
        return "none (no valid channel is selected)"
    return ", ".join(
        f"#{name} ({channel_id})" if name else channel_id
        for channel_id, name in sorted(scope.items(), key=lambda kv: (kv[1].lower(), kv[0]))
    )


def resolve_channel(value: Any, scope: Mapping[str, str]) -> str | None:
    """The selected channel ID *value* names (ID, ``#name`` or mention), or ``None``."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if mention := _MENTION_RE.match(raw):
        raw = mention.group(1)
    if raw.upper() in scope:
        return raw.upper()
    name = raw.lstrip("#").lower()
    if not name:
        return None
    matches = [channel_id for channel_id, label in scope.items() if label and label.lower() == name]
    return matches[0] if len(matches) == 1 else None


def _outside(value: Any, scope: Mapping[str, str]) -> str:
    return (
        f"Refused: Slack channel '{str(value).strip()}' is outside the Scope(s) set for this "
        f"agent or assignment. Allowed channels: {allowed_text(scope)}. Work only on those - "
        "do not retry with another channel."
    )


def _check(tool: str, kwargs: dict[str, Any], scope: Mapping[str, str]) -> tuple[dict[str, Any], str | None]:
    """*kwargs* with channel names resolved to IDs, or the reason to refuse."""
    if tool in CHANNEL_ARGUMENT_TOOLS:
        value = kwargs.get("channel")
        resolved = resolve_channel(value, scope)
        if resolved is None:
            return kwargs, _outside(value or "", scope)
        kwargs["channel"] = resolved
    elif tool == "upload_slack_file":
        raw = kwargs.get("channels")
        names = [part for part in str(raw or "").split(",") if part.strip()]
        if not names:
            return kwargs, _outside(raw or "", scope)
        resolved_ids: list[str] = []
        for name in names:
            resolved = resolve_channel(name, scope)
            if resolved is None:
                return kwargs, _outside(name, scope)
            resolved_ids.append(resolved)
        kwargs["channels"] = ",".join(dict.fromkeys(resolved_ids))
    elif tool == "list_slack_files":
        value = kwargs.get("channel")
        if isinstance(value, str) and value.strip():
            resolved = resolve_channel(value, scope)
            if resolved is None:
                return kwargs, _outside(value, scope)
            kwargs["channel"] = resolved
        elif len(scope) == 1:
            kwargs["channel"] = next(iter(scope))
        else:
            return kwargs, (
                "Refused: this agent or assignment is limited to a Slack Scope(s); name one of "
                f"the allowed channels in 'channel': {allowed_text(scope)}."
            )
    elif tool in SEARCH_TOOLS and not scope:
        return kwargs, (
            "Refused: this agent or assignment is limited to a Slack Scope(s) with no valid "
            "channel selected, so there is nothing to search."
        )
    elif tool == "list_slack_channels":
        # As many channels as the listing allows, so the selected ones show.
        kwargs["count"] = 200
    return kwargs, None


def _guarded(tool: str, handler: Any, scope: dict[str, str]) -> Any:
    @functools.wraps(handler)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        kwargs, refusal = _check(tool, dict(kwargs), scope)
        if refusal:
            logger.info("Slack Scope(s): refused a %s call", tool)
            return ToolResult(tool_call_id="", content=refusal, is_error=True)
        token = _CHANNEL_SCOPE.set(scope)
        try:
            return await handler(*args, **kwargs)
        finally:
            _CHANNEL_SCOPE.reset(token)

    return guarded


def apply_channel_scope(
    definitions: list[ToolDefinition],
    executor: CallableToolExecutor,
    scope: dict[str, str],
) -> list[ToolDefinition]:
    """Enforce *scope* on the Slack tools just registered on *executor*."""
    kept: list[ToolDefinition] = []
    note = f" Limited to this Scope(s): {allowed_text(scope)}."
    withdrawn = 0
    for definition in definitions:
        name = definition.name
        if name in DROPPED_WHEN_SCOPED:
            executor.unregister(name)
            withdrawn += 1
            continue
        handler = executor.handler(name)
        if handler is None:
            kept.append(definition)
            continue
        executor.register(name, _guarded(name, handler, scope))
        if name in SEARCH_TOOLS or name in ("list_slack_channels", "list_slack_files"):
            definition = dataclasses.replace(definition, description=definition.description + note)
        kept.append(definition)
    logger.info(
        "Slack Scope(s) enforced on %d channel(s): %d tool(s) withdrawn, %d tool(s) guarded",
        len(scope),
        withdrawn,
        len(kept),
    )
    return kept
