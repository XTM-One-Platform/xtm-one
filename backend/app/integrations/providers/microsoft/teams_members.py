"""Microsoft Teams team and channel member tools: add and remove members.

A team's members go through Graph ``/teams/{id}/members``; a private or shared
channel's through ``/teams/{id}/channels/{id}/members`` (a standard channel has
the team's members and takes none of its own: Graph refuses it and the answer
says so). A person is named by email address or Microsoft Entra user object ID
to add, and by email, user ID or membership ID to remove. Every change answers
with the member list read back, so the caller sees the result without a second
call.
"""

from __future__ import annotations

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _BASE_URL, _PROVIDER, MicrosoftContext
from ._teams import _channel_url, _team_url, _teams_segment, _validate_teams_channel_ids, _validate_teams_team_id
from .teams_chat_members import _FALSE_WORDS, _format_chat_members, _member_label, _member_matches, _parse_member_refs

#: The delegated Graph permissions the write tools need, named on a denial.
TEAM_MEMBER_PERMISSION = "TeamMember.ReadWrite.All"
CHANNEL_MEMBER_PERMISSION = "ChannelMember.ReadWrite.All"
#: The delegated permission ``list_microsoft_channel_members`` needs on a private or shared channel (#2188).
CHANNEL_MEMBER_READ_PERMISSION = "ChannelMember.Read.All"


def _permission_hint(err: str, permission: str) -> str:
    """Graph's error, naming the permission the tool needs when access was denied."""
    if "access denied" not in err:
        return err
    return (
        f"{err} This tool needs the delegated Microsoft Graph permission {permission}: an "
        "administrator adds it to the integration's app registration (API permissions, then "
        "consent) and the integration is re-authorized."
    )


def _as_owner(value: bool | str) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    return bool(value)


def register(ctx: MicrosoftContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the Teams team and channel member tools on *executor*."""
    _client = ctx.client
    _throttled_request = ctx.request

    async def _members(client, url: str, permission: str) -> tuple[list[dict], str | None]:
        members, err = await ctx.collect_all(client, f"{url}/members", "list members")
        if err:
            return [], _permission_hint(err, permission)
        return [m for m in members if isinstance(m, dict)], None

    async def _add(url: str, label: str, members: str | list, owner: bool | str, permission: str) -> str:
        refs, err = _parse_member_refs(members)
        if err:
            return err
        roles = ["owner"] if _as_owner(owner) else []
        async with _client() as client:
            current, err = await _members(client, url, permission)
            if err:
                return err
            already = [ref for ref in refs if any(_member_matches(m, ref) for m in current)]
            added: list[str] = []
            failed: list[str] = []
            for ref in refs:
                if ref in already:
                    continue
                _, add_err = await _throttled_request(
                    client,
                    "post",
                    f"{url}/members",
                    _PROVIDER,
                    f"add {label} member",
                    json={
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": roles,
                        "user@odata.bind": f"{_BASE_URL}/users('{ref}')",
                    },
                )
                if add_err:
                    failed.append(f"{ref}: {_permission_hint(add_err, permission)}")
                else:
                    added.append(ref)
            after, read_err = await _members(client, url, permission)
        parts: list[str] = []
        if added:
            parts.append(f"Added to the {label} as {'owner' if roles else 'member'}: {', '.join(added)}.")
        if already:
            parts.append(f"Already members, left unchanged: {', '.join(already)}.")
        if failed:
            parts.append("Not added:\n" + "\n".join(f"- {line}" for line in failed))
        parts.append(
            f"Reading the members back failed: {read_err}"
            if read_err
            else f"The {label} now has " + _format_chat_members(after)
        )
        result = "\n".join(parts)
        if not failed:
            return result
        # Error-shaped, so a partial add is never read as a success.
        lead = "not every member was added" if added or already else "nobody was added"
        return f"Error: {lead} to the {label}.\n{result}"

    async def _remove(url: str, label: str, member: str, permission: str) -> str:
        ref = member.strip() if isinstance(member, str) else ""
        if not ref:
            return "Error: member is empty: give an email address, a user object ID or a membership ID."
        async with _client() as client:
            current, err = await _members(client, url, permission)
            if err:
                return err
            match = next((m for m in current if _member_matches(m, ref)), None)
            if match is None or not match.get("id"):
                return f"Error: {ref!r} is not a member of the {label}; nothing was removed. It has " + (
                    _format_chat_members(current)
                )
            _, err = await _throttled_request(
                client,
                "delete",
                f"{url}/members/{_teams_segment(match['id'])}",
                _PROVIDER,
                f"remove {label} member",
            )
            if err:
                return _permission_hint(err, permission)
            after, read_err = await _members(client, url, permission)
        result = f"Removed {_member_label(match)} from the {label}."
        if read_err:
            return f"{result} Reading the members back failed: {read_err}"
        return f"{result} It now has " + _format_chat_members(after)

    async def add_microsoft_team_member(team_id: str, members: str | list = "", owner: bool | str = False) -> str:
        """Add people to a Microsoft Teams team."""
        if err := _validate_teams_team_id(team_id, "add_microsoft_team_member"):
            return err
        return await _add(_team_url(team_id), "team", members, owner, TEAM_MEMBER_PERMISSION)

    async def remove_microsoft_team_member(team_id: str, member: str = "") -> str:
        """Remove one person from a Microsoft Teams team."""
        if err := _validate_teams_team_id(team_id, "remove_microsoft_team_member"):
            return err
        return await _remove(_team_url(team_id), "team", member, TEAM_MEMBER_PERMISSION)

    async def add_microsoft_channel_member(
        team_id: str, channel_id: str, members: str | list = "", owner: bool | str = False
    ) -> str:
        """Add people to a private or shared Microsoft Teams channel."""
        if err := _validate_teams_channel_ids(team_id, channel_id, "add_microsoft_channel_member"):
            return err
        url = _channel_url(team_id, channel_id)
        return await _add(url, "channel", members, owner, CHANNEL_MEMBER_PERMISSION)

    async def remove_microsoft_channel_member(team_id: str, channel_id: str, member: str = "") -> str:
        """Remove one person from a private or shared Microsoft Teams channel."""
        if err := _validate_teams_channel_ids(team_id, channel_id, "remove_microsoft_channel_member"):
            return err
        return await _remove(_channel_url(team_id, channel_id), "channel", member, CHANNEL_MEMBER_PERMISSION)

    executor.register("add_microsoft_team_member", add_microsoft_team_member)
    executor.register("remove_microsoft_team_member", remove_microsoft_team_member)
    executor.register("add_microsoft_channel_member", add_microsoft_channel_member)
    executor.register("remove_microsoft_channel_member", remove_microsoft_channel_member)

    team_id = {"type": "string", "description": "The team ID (from list_microsoft_teams_and_channels)"}
    channel_id = {"type": "string", "description": "The channel ID (from list_microsoft_teams_and_channels)"}
    members = {
        "type": "string",
        "description": "Comma-separated email addresses or Microsoft Entra user object IDs to add",
    }
    member = {
        "type": "string",
        "description": "Email address, user object ID or membership id of the member to remove",
    }
    owner = {"type": "boolean", "description": "Add them as owners (default false: members)", "default": False}

    def _definition(name: str, description: str, properties: dict, required: list[str]) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": properties, "required": required},
            source="integration:microsoft",
            priority=1,
        )

    return [
        _definition(
            "add_microsoft_team_member",
            "Add people to a Microsoft Teams team, as members or owners. Returns the team's member list after the change.",
            {"team_id": team_id, "members": members, "owner": owner},
            ["team_id", "members"],
        ),
        _definition(
            "remove_microsoft_team_member",
            "Remove one person from a Microsoft Teams team. Returns the team's member list after the change.",
            {"team_id": team_id, "member": member},
            ["team_id", "member"],
        ),
        _definition(
            "add_microsoft_channel_member",
            "Add people to a private or shared Microsoft Teams channel (a standard channel has the "
            "team's members: add them to the team). Returns the channel's member list after the change.",
            {"team_id": team_id, "channel_id": channel_id, "members": members, "owner": owner},
            ["team_id", "channel_id", "members"],
        ),
        _definition(
            "remove_microsoft_channel_member",
            "Remove one person from a private or shared Microsoft Teams channel. Returns the channel's "
            "member list after the change.",
            {"team_id": team_id, "channel_id": channel_id, "member": member},
            ["team_id", "channel_id", "member"],
        ),
    ]
