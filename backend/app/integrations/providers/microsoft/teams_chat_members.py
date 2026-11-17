"""Microsoft Teams chat member tools: list, add and remove the members of an existing chat.

A group chat keeps its thread when people join or leave it: these tools never
create a chat. Adding goes through Graph ``POST /chats/{id}/members`` with the
chat's history shared from its creation by default; a one-on-one chat is
refused before any write, as Graph does not take new members there. Every
change answers with the chat's member list read back.
"""

from __future__ import annotations

import re

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _BASE_URL, _PROVIDER, MicrosoftContext
from ._teams import _teams_segment, _validate_teams_chat_id

#: The delegated Graph permission that lists, adds and removes chat members.
CHAT_MEMBER_PERMISSION = "ChatMember.ReadWrite"

#: ``visibleHistoryStartDateTime`` sharing a chat's whole history, when Graph
#: does not return the chat's creation time.
_ALL_HISTORY = "0001-01-01T00:00:00Z"

#: A member reference the add tool binds: an email address or an Entra object
#: ID. Neither form can hold a quote, a slash or a parenthesis, so the
#: ``users('...')`` reference it is put in stays one Graph entity reference.
_MEMBER_EMAIL_RE = re.compile(r"^[A-Za-z0-9._+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+$")
_MEMBER_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_FALSE_WORDS = frozenset({"false", "0", "no", "off", ""})


def _parse_member_refs(members: object) -> tuple[list[str], str | None]:
    """The distinct email addresses or Entra object IDs of *members*, or a refusal."""
    raw = members if isinstance(members, list) else str(members or "").split(",")
    refs: list[str] = []
    seen: set[str] = set()
    for item in raw:
        ref = str(item).strip()
        if not ref:
            continue
        if not (_MEMBER_EMAIL_RE.match(ref) or _MEMBER_GUID_RE.match(ref)):
            return [], (
                f"Error: {ref!r} is neither an email address nor a Microsoft Entra user object ID; "
                "nothing was changed."
            )
        if ref.lower() not in seen:
            seen.add(ref.lower())
            refs.append(ref)
    if not refs:
        return [], (
            "Error: members is empty: give one or more comma-separated email addresses "
            "or Microsoft Entra user object IDs."
        )
    return refs, None


def _permission_hint(err: str) -> str:
    """Graph's error, naming the permission the member tools need when access was denied."""
    if "access denied" not in err:
        return err
    return (
        f"{err} The chat member tools need the delegated Microsoft Graph permission "
        f"{CHAT_MEMBER_PERMISSION}: an administrator adds it to the integration's app "
        "registration (API permissions, then consent) and the integration is re-authorized."
    )


def _member_matches(member: dict, ref: str) -> bool:
    """Whether *ref* names *member*: its email, its Entra user ID or its membership ID."""
    key = ref.strip().lower()
    return bool(key) and key in {str(member.get(field) or "").lower() for field in ("email", "userId", "id")}


def _member_label(member: dict) -> str:
    """``Name <email>`` of a chat member."""
    return f"{member.get('displayName') or 'Unknown'} <{member.get('email') or 'no email'}>"


def _format_chat_members(members: list[dict]) -> str:
    """A chat's members, one line each: name, email, role, membership and user IDs."""
    if not members:
        return "no member listed by Microsoft."
    lines = [
        f"- {_member_label(m)}, role: {', '.join(m.get('roles') or []) or 'member'} "
        f"(membership id: {m.get('id', '')}, user id: {m.get('userId') or 'n/a'})"
        for m in members
    ]
    return f"{len(members)} member(s):\n" + "\n".join(lines)


def _chat_url(chat_id: str) -> str:
    """The Graph URL of a chat."""
    return f"{_BASE_URL}/chats/{_teams_segment(chat_id)}"


def register(ctx: MicrosoftContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the Teams chat member tools on *executor* and return their definitions."""
    _client = ctx.client
    _throttled_request = ctx.request

    async def _chat_members(client, chat_id: str) -> tuple[list[dict], str | None]:
        """Every member of a chat, across Graph's pages (``@odata.nextLink``), or Graph's error."""
        members, err = await ctx.collect_all(client, f"{_chat_url(chat_id)}/members", "list chat members")
        if err:
            return [], _permission_hint(err)
        return [m for m in members if isinstance(m, dict)], None

    async def list_microsoft_chat_members(chat_id: str) -> str:
        """List the members of a Microsoft Teams chat: name, email, role and membership ID."""
        if err := _validate_teams_chat_id(chat_id, "list_microsoft_chat_members"):
            return err
        async with _client() as client:
            members, err = await _chat_members(client, chat_id)
        if err:
            return err
        return f"Chat {chat_id.strip()} has " + _format_chat_members(members)

    async def add_microsoft_chat_members(
        chat_id: str,
        members: str | list = "",
        include_history: bool | str = True,
    ) -> str:
        """Add people to an existing Microsoft Teams group chat; never creates a chat."""
        if err := _validate_teams_chat_id(chat_id, "add_microsoft_chat_members"):
            return err
        refs, err = _parse_member_refs(members)
        if err:
            return err
        if isinstance(include_history, str):
            include_history = include_history.strip().lower() not in _FALSE_WORDS
        async with _client() as client:
            chat_resp, err = await _throttled_request(
                client,
                "get",
                _chat_url(chat_id),
                _PROVIDER,
                "read chat",
                params={"$select": "id,chatType,topic,createdDateTime"},
            )
            if err:
                return _permission_hint(err)
            payload = chat_resp.json() if chat_resp is not None else None
            chat = payload if isinstance(payload, dict) else {}
            if chat.get("chatType") == "oneOnOne":
                return (
                    "Error: this is a one-on-one chat, and Microsoft Teams does not allow adding "
                    "members to a one-on-one chat; nothing was changed. Only a group chat takes "
                    "new members."
                )
            current, err = await _chat_members(client, chat_id)
            if err:
                return err
            already = [ref for ref in refs if any(_member_matches(m, ref) for m in current)]
            history = (chat.get("createdDateTime") or _ALL_HISTORY) if include_history else None
            added: list[str] = []
            failed: list[str] = []
            for ref in refs:
                if ref in already:
                    continue
                body: dict = {
                    "@odata.type": "#microsoft.graph.aadUserConversationMember",
                    "roles": ["owner"],
                    "user@odata.bind": f"{_BASE_URL}/users('{ref}')",
                }
                if history:
                    body["visibleHistoryStartDateTime"] = history
                _, add_err = await _throttled_request(
                    client,
                    "post",
                    f"{_chat_url(chat_id)}/members",
                    _PROVIDER,
                    "add chat member",
                    json=body,
                )
                if add_err:
                    failed.append(f"{ref}: {_permission_hint(add_err)}")
                else:
                    added.append(ref)
            after, read_err = await _chat_members(client, chat_id)
        parts: list[str] = []
        if added:
            seen = "with the chat history" if history else "without the earlier history"
            parts.append(f"Added to chat {chat_id.strip()} ({seen}): {', '.join(added)}.")
        if already:
            parts.append(f"Already members, left unchanged: {', '.join(already)}.")
        if failed:
            parts.append("Not added:\n" + "\n".join(f"- {line}" for line in failed))
        if read_err:
            parts.append(f"Reading the members back failed: {read_err}")
        else:
            parts.append("The chat now has " + _format_chat_members(after))
        result = "\n".join(parts)
        if not failed:
            return result
        if added or already:
            # Error-shaped, so a partial add is never read as a success.
            return f"Error: not every member was added to chat {chat_id.strip()}.\n{result}"
        return f"Error: nobody was added to chat {chat_id.strip()}.\n{result}"

    async def remove_microsoft_chat_member(chat_id: str, member: str = "") -> str:
        """Remove one person from a Microsoft Teams group chat, an email resolved to its membership."""
        if err := _validate_teams_chat_id(chat_id, "remove_microsoft_chat_member"):
            return err
        ref = member.strip() if isinstance(member, str) else ""
        if not ref:
            return "Error: member is empty: give an email address, a user object ID or a membership ID."
        async with _client() as client:
            current, err = await _chat_members(client, chat_id)
            if err:
                return err
            match = next((m for m in current if _member_matches(m, ref)), None)
            if match is None or not match.get("id"):
                return (
                    f"Error: {ref!r} is not a member of chat {chat_id.strip()}; nothing was removed. "
                    "The chat has " + _format_chat_members(current)
                )
            _, err = await _throttled_request(
                client,
                "delete",
                f"{_chat_url(chat_id)}/members/{_teams_segment(match['id'])}",
                _PROVIDER,
                "remove chat member",
            )
            if err:
                return _permission_hint(err)
            after, read_err = await _chat_members(client, chat_id)
        result = f"Removed {_member_label(match)} from chat {chat_id.strip()}."
        if read_err:
            return f"{result} Reading the members back failed: {read_err}"
        return f"{result} The chat now has " + _format_chat_members(after)

    executor.register("list_microsoft_chat_members", list_microsoft_chat_members)
    executor.register("add_microsoft_chat_members", add_microsoft_chat_members)
    executor.register("remove_microsoft_chat_member", remove_microsoft_chat_member)

    chat_id_schema = {
        "type": "string",
        "description": "The chat ID (from list_microsoft_chats or create_microsoft_chat)",
    }
    return [
        ToolDefinition(
            name="list_microsoft_chat_members",
            description=(
                "List the members of a Microsoft Teams chat (1:1 or group): name, email, role, "
                "membership id and user id."
            ),
            input_schema={
                "type": "object",
                "properties": {"chat_id": chat_id_schema},
                "required": ["chat_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="add_microsoft_chat_members",
            description=(
                "Add people to an existing Microsoft Teams group chat, keeping the same thread "
                "(never creates a chat). With include_history (default true) they see the whole "
                "conversation since the chat was created. A one-on-one chat cannot take new "
                "members. Returns the chat's member list after the change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": chat_id_schema,
                    "members": {
                        "type": "string",
                        "description": "Comma-separated email addresses or Microsoft Entra user object IDs to add",
                    },
                    "include_history": {
                        "type": "boolean",
                        "description": "Share the chat history since its creation with the new members (default true)",
                        "default": True,
                    },
                },
                "required": ["chat_id", "members"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="remove_microsoft_chat_member",
            description=(
                "Remove one person from a Microsoft Teams group chat, by email address, user "
                "object ID or the membership id list_microsoft_chat_members returns. Returns the "
                "chat's member list after the change."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": chat_id_schema,
                    "member": {
                        "type": "string",
                        "description": "Email address, user object ID or membership id of the member to remove",
                    },
                },
                "required": ["chat_id", "member"],
            },
            source="integration:microsoft",
            priority=1,
        ),
    ]
