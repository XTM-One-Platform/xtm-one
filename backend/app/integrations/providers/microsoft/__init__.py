"""Microsoft integration provider package (Microsoft Graph, delegated OAuth).

Outlook mail and calendar, Teams chats, channels and meetings, OneDrive,
SharePoint and To Do for the signed-in user. Split by domain (mirrors the
``notion`` and ``github`` packages):

* ``_common``        - Graph constants, :class:`MicrosoftContext` (the session's
                       auto-refreshing client, throttled requests, pagination,
                       injected callbacks) and the shared parsing helpers.
* ``_outlook``       - mail and calendar helpers: message ids, ``$search``
                       quoting, the signature, event URLs, attachments.
* ``_drive``         - drive-item addressing, links and the readers shared by
                       the OneDrive and SharePoint tools.
* ``_teams``         - Teams id checks, message links and chat / channel message
                       rendering (listings cut only what does not fit).
* ``_mentions``      - the ``mentions`` argument of the Teams message tools,
                       resolved against Graph.
* ``_meetings``      - online-meeting ids from join URLs; Teams meeting options
                       (found by join URL, PATCHed, read back).
* ``_recurrence``    - the ``recurrence`` argument as a Graph patternedRecurrence.
* ``mail``           - read, search, flag, draft, reply and send email.
* ``calendar``       - calendars, events, free/busy, recurring series and their
                       occurrences, invitation responses, Teams meeting options.
* ``files``          - OneDrive reading, download / upload, Word comments,
                       Teams file sends.
* ``drive_ops``      - drive file upkeep: rename, move, delete (recycle bin),
                       export to PDF.
* ``sharepoint``     - sites and their files.
* ``tasks``          - Microsoft To Do.
* ``teams_channels`` - teams, channels, channel messages and members.
* ``teams_chats``    - chats and chat messages.
* ``teams_chat_members`` - the members of an existing chat: list, add, remove.
 * ``teams_members``  - the members of a team or a private / shared channel: add, remove.
* ``meetings``       - online meetings, attendance, recordings, AI insights.
* ``transcripts``    - meeting transcripts, and the transcripts embedded in
                       recordings (the user's OneDrive, channel folders).
* ``teams_scope``    - the Teams channel Scope(s) boundary of a run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import (
    calendar,
    drive_ops,
    files,
    mail,
    meetings,
    sharepoint,
    tasks,
    teams_channels,
    teams_chat_members,
    teams_chats,
    teams_members,
    transcripts,
)
from ._chat_file_markers import wrap_chat_send_file_markers
from ._common import _BASE_URL, GRAPH_API, MicrosoftContext
from ._drive import _drive_item_link_lines, _encode_share_id

# ``teams_scope`` is imported where it is used, never at package import time:
# importlib holds a submodule's lock while it imports the parent package, so
# a thread importing ``microsoft.teams_scope`` directly (``element_scope``,
# ``integration_tools``) while another thread ran this ``__init__`` deadlocked
# both on first use ("deadlock detected by _ModuleLock", a run failed at its
# start). Same rule as ``app.services.openapi_import``.

__all__ = [
    "GRAPH_API",
    "MicrosoftContext",
    "MicrosoftIntegration",
    "_drive_item_link_lines",
    "_encode_share_id",
    "build_tools",
]

# Registration order is the order the tools are listed to the model.
_DOMAINS = (
    mail,
    calendar,
    files,
    drive_ops,
    sharepoint,
    tasks,
    teams_channels,
    teams_chats,
    teams_chat_members,
    teams_members,
    meetings,
    transcripts,
)


class MicrosoftIntegration(BaseIntegration):
    provider_name = "microsoft"
    display_name = "Microsoft (Outlook & Teams)"
    icon = "MessageSquare"
    description = "Connect Outlook for email triage and Microsoft Teams for messaging."
    auth_types = ["oauth"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        return OAuthDefaults(
            authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",  # noqa: S106
            # OAuth token exchange endpoint URL, not a secret.
            scopes=[
                "openid",
                "profile",
                "email",
                "offline_access",
                # Mail
                "Mail.Read",
                "Mail.ReadWrite",
                "Mail.Send",
                # Teams – chat
                "Chat.Create",
                "Chat.Read",
                "Chat.ReadWrite",
                "ChatMember.ReadWrite",
                "ChatMessage.Read",
                "ChatMessage.Send",
                # Teams – teams & channels
                "Team.ReadBasic.All",
                "Channel.Create",
                "Channel.ReadBasic.All",
                "ChannelMember.Read.All",
                # Add and remove team and channel members (admin consent in
                # most tenants: optional in the catalog).
                "ChannelMember.ReadWrite.All",
                "TeamMember.ReadWrite.All",
                "ChannelMessage.Read.All",
                "ChannelMessage.Send",
                # Calendar (own + calendars others have shared with the user;
                # the .Shared variants are REQUIRED for delegate access to
                # coworkers' calendars added to the user's Outlook).
                "Calendars.Read",
                "Calendars.ReadWrite",
                "Calendars.Read.Shared",
                "Calendars.ReadWrite.Shared",
                # OneDrive / Files
                "Files.Read.All",
                "Files.ReadWrite.All",
                # SharePoint
                "Sites.Read.All",
                "Sites.ReadWrite.All",
                # Groups (enumerate the SharePoint sites backing the user's
                # Microsoft 365 groups / Teams via /me/memberOf)
                "GroupMember.Read.All",
                # Tasks (Microsoft To Do)
                "Tasks.Read",
                "Tasks.ReadWrite",
                # Online Meetings (ReadWrite also sets a calendar meeting's
                # recording and transcription options)
                "OnlineMeetings.Read",
                "OnlineMeetings.ReadWrite",
                "OnlineMeetingArtifact.Read.All",
                "OnlineMeetingRecording.Read.All",
                "OnlineMeetingAiInsight.Read.All",
                # Call & Meeting Transcripts
                "CallTranscripts.Read.All",
                "OnlineMeetingTranscript.Read.All",
            ],
        )

    async def validate_connection(self, token: str) -> bool:
        async with egress_client() as client:
            resp = await client.get(
                f"{_BASE_URL}/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            return resp.status_code == 200

    async def get_user_info(self, token: str) -> dict:
        async with egress_client() as client:
            resp = await client.get(
                f"{_BASE_URL}/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            return resp.json()


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    *,
    refresh_fn: Callable[[], Awaitable[str | None]] | None = None,
    sp_token_fn: Callable[[str], Awaitable[str | None]] | None = None,
    **kwargs: Any,
) -> list[ToolDefinition]:
    """Build every Microsoft tool and register its callable on *executor*.

    ``integration_tools`` injects ``persist_file_fn`` (keep a downloaded file
    as a file of the conversation or run), ``integration_id`` (the mailbox a
    send goes through), ``agent_id`` (the agent the mail send guard is scoped
    to), ``teams_channel_scope`` (the run's Teams channel
    Scope(s), a hard boundary), ``signature_fn`` (the person's
    signature from the integration's signature integration) and
    ``signature_plan`` (its Email signature setting as resolved) and
    ``send_origin`` (what produced the run, logged with every message sent
    through an agent account on the integration) and ``compose_style`` (the
    integration's Compose style field, the CSS of the text the mail tools write).
    """
    ctx = MicrosoftContext(
        token,
        refresh_fn,
        persist_file_fn=kwargs.get("persist_file_fn"),
        integration_id=kwargs.get("integration_id"),
        agent_id=kwargs.get("agent_id"),
        sp_token_fn=sp_token_fn,
        signature_fn=kwargs.get("signature_fn"),
        signature_plan=kwargs.get("signature_plan"),
        send_origin=kwargs.get("send_origin"),
        compose_style=kwargs.get("compose_style"),
    )
    definitions: list[ToolDefinition] = []
    for domain in _DOMAINS:
        definitions += domain.register(ctx, executor)
    wrap_chat_send_file_markers(executor)
    teams_channel_scope = kwargs.get("teams_channel_scope")
    if teams_channel_scope is not None:
        from .teams_scope import apply_teams_channel_scope

        return apply_teams_channel_scope(definitions, executor, teams_channel_scope)
    return definitions
