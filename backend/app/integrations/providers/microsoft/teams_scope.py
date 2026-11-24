"""Hard channel boundary for the Microsoft Teams tools.

The Teams channels an operator selects in an agent's or an assignment's
Scope(s) (``integration_element_bindings["microsoft:teams_channel"]``) used to
be prompt text only: the Teams tools accepted any team and channel the
delegated token could reach. With a Teams channel scope in the run's
effective scope (``services.element_scope``), ``build_tools`` passes the tools
through :func:`apply_teams_channel_scope`, the same shape as the GitHub
repository guard (``providers/github/scope.py``):

* a call on a channel - ``team_id`` + ``channel_id``, the Teams message link
  ``get_microsoft_channel_message`` takes instead, or the channel meeting
  tools' ``team`` + ``channel`` (a display name or an ID) - that is not a
  selected channel is refused before any request, naming the allowed
  channels. A link is read for its IDs and a name is resolved to the selected
  channel's IDs, never looked up in Microsoft Graph;
* ``list_microsoft_teams_and_channels`` answers with the selected channels;
* the tools that reach Teams conversations outside any channel selection are
  withdrawn: every chat tool (1:1 and group chats) and
  ``create_microsoft_channel`` (a new channel is outside the selection by
  definition).

The selection applies to Teams only: Outlook, calendar, OneDrive, SharePoint,
To Do and the user's own online meetings are not Teams channels and keep
their tools. A selection with no usable channel allows nothing.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
from collections.abc import Mapping
from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._teams import _channel_message_target

logger = logging.getLogger(__name__)

#: Element-binding key of a Teams channel scope (``integration_elements``).
TEAMS_CHANNEL_SCOPE_KEY = "microsoft:teams_channel"

#: Tools that reach Teams conversations outside any channel selection.
DROPPED_WHEN_SCOPED = frozenset(
    {
        "create_microsoft_channel",
        "list_microsoft_chats",
        "list_microsoft_chat_messages",
        "list_all_microsoft_chat_messages",
        "get_microsoft_chat_message",
        "send_microsoft_chat_message",
        "update_microsoft_chat_message",
        "delete_microsoft_chat_message",
        "add_microsoft_chat_message_reaction",
        "remove_microsoft_chat_message_reaction",
        "create_microsoft_chat",
        "list_microsoft_chat_members",
        "add_microsoft_chat_members",
        "remove_microsoft_chat_member",
        "send_microsoft_chat_file",
        "download_microsoft_chat_message_image",
        "add_microsoft_team_member",
        "remove_microsoft_team_member",
    }
)

#: Tools addressing one channel by ``team_id`` + ``channel_id``.
CHANNEL_ID_TOOLS = frozenset(
    {
        "list_microsoft_channel_messages",
        "get_microsoft_channel_message",
        "send_microsoft_channel_message",
        "update_microsoft_channel_message",
        "delete_microsoft_channel_message",
        "add_microsoft_channel_message_reaction",
        "remove_microsoft_channel_message_reaction",
        "reply_microsoft_channel_message",
        "list_microsoft_channel_members",
        "add_microsoft_channel_member",
        "remove_microsoft_channel_member",
        "send_microsoft_channel_file",
        "download_microsoft_channel_message_image",
    }
)

#: Channel tools that take a Teams message link (``url``) instead of the IDs:
#: the link is read for them before the channel is checked.
CHANNEL_LINK_TOOLS = frozenset({"get_microsoft_channel_message"})

#: Tools addressing one channel by ``team`` + ``channel`` (name or ID).
CHANNEL_NAME_TOOLS = frozenset(
    {
        "get_microsoft_channel_meeting_recordings",
        "get_microsoft_channel_meeting_transcript",
    }
)

LISTING_TOOL = "list_microsoft_teams_and_channels"


@dataclasses.dataclass(frozen=True)
class ScopedChannel:
    team_id: str
    channel_id: str
    team_name: str = ""
    channel_name: str = ""

    def label(self) -> str:
        team = self.team_name or self.team_id
        channel = self.channel_name or self.channel_id
        return f"{team} / {channel} (team_id: {self.team_id}, channel_id: {self.channel_id})"


def teams_channel_scope_from_bindings(
    element_scope: Mapping[str, Any] | None,
) -> tuple[ScopedChannel, ...] | None:
    """The selected channels, or ``None`` when no Teams channel scope is set.

    *element_scope* is a run's EFFECTIVE scope (``services.element_scope``).
    The team and channel come from the element ID (``team_id:channel_id``, as
    the Scope(s) picker writes it), never from its metadata; names are for
    display and name resolution only. A selection with no usable entry -
    including the empty list a narrowing produces - allows nothing.
    """
    if not isinstance(element_scope, Mapping):
        return None
    items = element_scope.get(TEAMS_CHANNEL_SCOPE_KEY)
    if items is None:
        return None
    if not isinstance(items, list):
        return ()
    channels: dict[tuple[str, str], ScopedChannel] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        element_id = item.get("id")
        if not isinstance(element_id, str) or ":" not in element_id:
            continue
        team_id, _, channel_id = element_id.strip().partition(":")
        if not team_id.strip() or not channel_id.strip():
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        team_name = item.get("group") or metadata.get("team_name") or ""
        channel_name = item.get("name") or ""
        channels[(team_id.strip().lower(), channel_id.strip().lower())] = ScopedChannel(
            team_id=team_id.strip(),
            channel_id=channel_id.strip(),
            team_name=team_name if isinstance(team_name, str) else "",
            channel_name=channel_name if isinstance(channel_name, str) else "",
        )
    return tuple(channels.values())


def allowed_text(scope: tuple[ScopedChannel, ...]) -> str:
    if not scope:
        return "none (no valid channel is selected)"
    return "; ".join(channel.label() for channel in scope)


def _norm(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def resolve_by_ids(team_id: Any, channel_id: Any, scope: tuple[ScopedChannel, ...]) -> ScopedChannel | None:
    team, channel = _norm(team_id), _norm(channel_id)
    for scoped in scope:
        if scoped.team_id.lower() == team and scoped.channel_id.lower() == channel:
            return scoped
    return None


def resolve_by_names(team: Any, channel: Any, scope: tuple[ScopedChannel, ...]) -> ScopedChannel | None:
    """The selected channel a team + channel (each a name or an ID) names."""
    team_key, channel_key = _norm(team), _norm(channel)
    if not team_key or not channel_key:
        return None
    matches = [
        scoped
        for scoped in scope
        if team_key in (scoped.team_id.lower(), scoped.team_name.lower())
        and channel_key in (scoped.channel_id.lower(), scoped.channel_name.lower())
    ]
    return matches[0] if len(matches) == 1 else None


def _outside(team: Any, channel: Any, scope: tuple[ScopedChannel, ...]) -> str:
    return (
        f"Refused: Teams channel '{str(team or '').strip()} / {str(channel or '').strip()}' is "
        "outside the Scope(s) set for this agent or assignment. Allowed channels: "
        f"{allowed_text(scope)}. Work only on those - do not retry with another channel."
    )


def scoped_listing(scope: tuple[ScopedChannel, ...]) -> str:
    """The teams-and-channels listing a scoped run sees: its selection."""
    if not scope:
        return (
            "No channel is available: this agent or assignment is limited to a Teams "
            "Scope(s) with no valid channel selected."
        )
    lines = [
        f"Channels in this agent's or assignment's Scope(s) ({len(scope)}); no other "
        "channel may be used:"
    ]
    teams: dict[str, list[ScopedChannel]] = {}
    for scoped in scope:
        teams.setdefault(scoped.team_id, []).append(scoped)
    for team_id, channels in teams.items():
        lines.append(f"## Team: {channels[0].team_name or team_id} (ID: {team_id})")
        lines.extend(f"  - {c.channel_name or c.channel_id} (ID: {c.channel_id})" for c in channels)
    return "\n".join(lines)


def _check(tool: str, kwargs: dict[str, Any], scope: tuple[ScopedChannel, ...]) -> tuple[dict[str, Any], str | None]:
    if tool in CHANNEL_LINK_TOOLS and kwargs.get("url"):
        target, err = _channel_message_target(
            kwargs.get("team_id"),
            kwargs.get("channel_id"),
            kwargs.get("message_id"),
            kwargs.get("parent_message_id"),
            kwargs.pop("url"),
            tool,
        )
        if err:
            return kwargs, err
        kwargs.update(target)
    if tool in CHANNEL_ID_TOOLS:
        scoped = resolve_by_ids(kwargs.get("team_id"), kwargs.get("channel_id"), scope)
        if scoped is None:
            return kwargs, _outside(kwargs.get("team_id"), kwargs.get("channel_id"), scope)
        kwargs["team_id"], kwargs["channel_id"] = scoped.team_id, scoped.channel_id
    elif tool in CHANNEL_NAME_TOOLS:
        scoped = resolve_by_names(kwargs.get("team"), kwargs.get("channel"), scope)
        if scoped is None:
            return kwargs, _outside(kwargs.get("team"), kwargs.get("channel"), scope)
        kwargs["team"], kwargs["channel"] = scoped.team_id, scoped.channel_id
    return kwargs, None


def _guarded(tool: str, handler: Any, scope: tuple[ScopedChannel, ...]) -> Any:
    @functools.wraps(handler)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        if tool == LISTING_TOOL:
            return scoped_listing(scope)
        kwargs, refusal = _check(tool, dict(kwargs), scope)
        if refusal:
            logger.info("Teams Scope(s): refused a %s call", tool)
            return ToolResult(tool_call_id="", content=refusal, is_error=True)
        return await handler(*args, **kwargs)

    return guarded


def apply_teams_channel_scope(
    definitions: list[ToolDefinition],
    executor: CallableToolExecutor,
    scope: tuple[ScopedChannel, ...],
) -> list[ToolDefinition]:
    """Enforce *scope* on the Teams tools just registered on *executor*."""
    guarded_names = CHANNEL_ID_TOOLS | CHANNEL_NAME_TOOLS | {LISTING_TOOL}
    note = f" Limited to this Scope(s): {allowed_text(scope)}."
    kept: list[ToolDefinition] = []
    withdrawn = 0
    for definition in definitions:
        name = definition.name
        if name in DROPPED_WHEN_SCOPED:
            executor.unregister(name)
            withdrawn += 1
            continue
        handler = executor.handler(name) if name in guarded_names else None
        if handler is not None:
            executor.register(name, _guarded(name, handler, scope))
            if name == LISTING_TOOL:
                definition = dataclasses.replace(definition, description=definition.description + note)
        kept.append(definition)
    logger.info(
        "Teams Scope(s) enforced on %d channel(s): %d tool(s) withdrawn",
        len(scope),
        withdrawn,
    )
    return kept
