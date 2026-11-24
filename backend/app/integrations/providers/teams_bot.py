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

"""Teams Bot channel tools – Entra ID (Azure AD) + Microsoft Graph + Bot Framework.

These tools let agents send and read messages **as the bot** in Microsoft
Teams channels and chats.  They are separate from the ``microsoft``
integration tools (which operate under a user-delegated OAuth token and
cover email, calendar, OneDrive, etc.).

**Read operations** (list teams/channels, read messages) use the Microsoft
Graph API with the ``https://graph.microsoft.com/.default`` scope.

**Channel write operations** (send/reply channel messages) use the Bot
Framework with the ``https://api.botframework.com/.default`` scope, because
the Graph API's ``POST /teams/{id}/channels/{id}/messages`` endpoint does
not support standard application permissions (``ChannelMessage.Send`` only
exists as a delegated permission).

**Chat operations** follow the same split: reads go through Graph with the
app-only-supported endpoints (``GET /users/{email}/chats`` with
``Chat.ReadBasic.All`` or higher, ``GET /chats/{id}/messages`` with
``Chat.Read.All`` / ``ChatMessage.Read.All``), while writes go through Bot
Framework — Graph supports NO application permission for sending chat
messages (only the ``Teamwork.Migrate.All`` import mode), so 1:1 sends use
proactive conversation creation and group-chat sends post into a
conversation the bot app was added to (roster membership).

Credential format:  ``tenant_id||client_id||client_secret``  (passed by
``integration_tools._get_token``).  The builder exchanges these for
short-lived access tokens via the Entra ID client-credentials grant
using the tenant-specific endpoint.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from typing import Any
from urllib.parse import quote as _url_quote

import httpx

from app.channels.base import sanitize_md_link_text
from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.file_delivery import untrusted_file_facts, uploaded_file_answer
from app.integrations.http import (
    app_token_cache_key,
    get_cached_app_token,
    is_publish_allowed,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)
from app.integrations.providers.graph_path import _validate_path_segment
from app.integrations.providers.microsoft import (
    GRAPH_API,
    _drive_item_link_lines,
    _encode_share_id,
)
from app.llm.base import FileRef
from app.services.agent_posts import record_teams_channel_post
from app.services.teams_format import (
    ensure_full_width_card as _ensure_full_width_card,
)
from app.services.teams_format import markdown_to_teams_html, markdown_to_teams_text
from app.tools.base import CallableToolExecutor, FileContext, SendGuard, ToolDefinition, ToolResult, WriteCoalescing

logger = logging.getLogger(__name__)

_PROVIDER = "Teams Bot"
_SOURCE = "integration:teams_bot"

#: Max bytes fetched/uploaded by the file tools (matches the Notion cap).
_TEAMS_MAX_FILE_BYTES = 100 * 1024 * 1024


def _format_graph_attachments(msg: dict) -> str:
    """Render a Graph chatMessage's file attachments as a compact suffix.

    Files shared in chats/channels arrive as ``reference`` attachments with
    a SharePoint ``contentUrl`` — surfacing them lets the agent download the
    bytes via ``download_teams_bot_file``.  Returns ``""`` when the message
    has no file attachments.
    """
    parts: list[str] = []
    for att in msg.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        content_url = att.get("contentUrl")
        if att.get("contentType") == "reference" and content_url:
            parts.append(f"{att.get('name') or 'file'} <{content_url}>")
    if not parts:
        return ""
    return (
        " [attached files — fetch with download_teams_bot_file: "
        + ", ".join(parts)
        + "]"
    )


def _graph_share_token(url: str) -> str:
    """Encode a SharePoint/OneDrive URL for the Graph ``/shares`` API.

    Unpadded base64url of the UTF-8 URL with the documented ``u!`` prefix
    (delegates to the canonical :func:`_encode_share_id` implementation).
    """
    return _encode_share_id(url)

# Default Bot Framework service URL for Teams Online (Americas region).
# The channel adapter caches the actual region-specific URL from inbound
# messages in Redis (key ``teams:service_url:{tenant_id}``).
_DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/amer/"

#: Graph caps ``$top`` at 50 for chat/message listings.
_MAX_PAGE_SIZE = 50


def _validate_thread_conversation_ids(
    channel_id: str, reply_to_id: str
) -> str | None:
    """Validate the ids embedded in a threaded conversation id.

    Channel thread replies POST to ``{channel_id};messageid={reply_to_id}``.
    Both values are LLM-controlled tool input, so on top of the generic
    path-segment check, ``;`` is rejected explicitly: an embedded ``;``
    would inject extra conversation-id suffix segments into the reply URL.
    (``_validate_path_segment`` cannot reject ``;`` globally — full chat
    conversation ids legitimately carry ``;messageid=`` suffixes.)
    """
    for value, label in ((channel_id, "channel ID"), (reply_to_id, "reply_to_id")):
        if invalid := _validate_path_segment(value, label):
            return invalid
        if ";" in value:
            return f"Error: Invalid {label}: {value!r}."
    return None


def _clamp_count(count: Any, default: int = 20) -> int:
    """Clamp an LLM-supplied page size to Graph's supported 1..50 range."""
    try:
        return max(1, min(int(count), _MAX_PAGE_SIZE))
    except (TypeError, ValueError):
        return default


def _token_url(tenant_id: str) -> str:
    """Return the Entra ID token endpoint for the given tenant.

    Falls back to the legacy ``botframework.com`` tenant if no tenant_id
    is supplied (backwards-compatible with existing integrations).
    """
    tid = tenant_id or "botframework.com"
    return f"https://login.microsoftonline.com/{tid}/oauth2/v2.0/token"


async def _acquire_token(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    *,
    scope: str,
) -> str | None:
    """Exchange client credentials for an access token via Entra ID.

    Parameters
    ----------
    scope:
        ``https://graph.microsoft.com/.default`` for Graph API reads, or
        ``https://api.botframework.com/.default`` for Bot Framework writes.

    Tokens are cached in the shared process-wide app-token cache (the same
    one Defender/Sentinel/Entra use) so a fresh Entra ID client-credentials
    exchange is NOT performed on every inbound Teams message / Graph call.
    """
    cache_key = app_token_cache_key("teams_bot", tenant_id, client_id, client_secret, scope)
    cached = get_cached_app_token(cache_key)
    if cached:
        return cached

    async with egress_client(timeout=30) as client:
        resp, err = await safe_request(
            client,
            "post",
            _token_url(tenant_id),
            _PROVIDER,
            f"acquire token (scope={scope})",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scope,
            },
        )
        if resp is None:
            logger.warning("Failed to acquire token (scope=%s): %s", scope, err)
            return None
        data = resp.json()
        token = data.get("access_token")
        if token:
            set_cached_app_token(cache_key, token, data.get("expires_in"))
        return token


async def _get_service_url(tenant_id: str) -> str:
    """Look up the Bot Framework service URL for a tenant.

    Checks Redis cache first (populated by the Teams channel adapter on
    inbound messages), then falls back to the default regional URL.
    """
    if tenant_id:
        try:
            from app.core.redis import redis_client

            redis = await redis_client()
            key = f"teams:service_url:{tenant_id}"
            url = await redis.get(key)
            if url:
                result = url if isinstance(url, str) else url.decode()
                return result.rstrip("/")
        except Exception as exc:
            logger.warning("Failed to read Teams service URL from Redis: %s", format_exception_message(exc))
    return _DEFAULT_SERVICE_URL.rstrip("/")


# ---------------------------------------------------------------------------
# Full-width content delivery: chat surfaces vs channels
# ---------------------------------------------------------------------------
#
# ``msteams.width = "Full"`` only makes a card render edge-to-edge in a Team
# **channel**.  In every **chat** surface (1:1, group chat, meeting chat)
# Teams hard-caps an Adaptive Card to the message-bubble width regardless of
# the width hint — this is documented, by-design Microsoft behaviour, not a
# bug we can fix card-side:
#
#   https://learn.microsoft.com/en-us/answers/questions/5655250
#   - Channel: 100% width of entire message, edge to edge.
#   - Chat:    100% of the *message-bubble* max width (with a right indent);
#              there is no card property to span the full chat pane.
#
# A plain Markdown **text** message, however, renders at full conversation
# width on *every* surface.  So for a display-only ("content") card destined
# for a chat we render it as full-width Markdown text instead of shipping a
# permanently-narrow card.  Interactive cards (buttons that submit data,
# inputs, expandable sub-cards) MUST stay cards — Markdown cannot represent
# them — and real channels keep their (full-width) cards.

# Conversation-id suffixes that identify a real Team *channel*.  Mirrors
# Microsoft's own teams.ts ``supportsThreading()`` channel set.  Detection is
# deliberately conservative: anything not provably a channel is treated as a
# chat, so the worst case is a channel card degrading to full-width Markdown
# (still full width) rather than a chat card staying bubble-constrained (the
# very regression we are fixing).
_TEAMS_CHANNEL_CONVERSATION_SUFFIXES = ("@thread.tacv2", "@thread.skype")

# Adaptive Card element/action types that REQUIRE a real card to render —
# Markdown text cannot express data-submitting buttons, inline inputs,
# expandable sub-cards, or visibility toggles.  ``Action.OpenUrl`` is just a
# hyperlink, so it is intentionally NOT here (it renders fine as a Markdown
# link).
_INTERACTIVE_ACTION_TYPES = frozenset(
    {
        "Action.Submit",
        "Action.Execute",
        "Action.ShowCard",
        "Action.ToggleVisibility",
    }
)

# Display-only element types that nevertheless REQUIRE native card rendering —
# Markdown cannot represent them, so downgrading a card containing them to
# text would silently destroy its main content.  Charts (Chart.Donut,
# Chart.Pie, Chart.VerticalBar[.Grouped], Chart.HorizontalBar[.Stacked],
# Chart.Line, Chart.Gauge) are official Teams host extensions rendered
# natively on every surface, including group/meeting chats:
# https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/charts-in-adaptive-cards
_NATIVE_ONLY_ELEMENT_PREFIXES = ("Chart.",)
_NATIVE_ONLY_ELEMENT_TYPES = frozenset(
    {
        "Media",
        "CodeBlock",
        "Table",
        "Carousel",
    }
)

# Conversation-id suffixes that identify a *chat* surface (group chat,
# meeting chat, 1:1).  Chat conversation ids are addressed directly via
# ``POST /v3/conversations/{id}/activities`` — no team/channel routing.
_TEAMS_CHAT_CONVERSATION_SUFFIXES = ("@thread.v2", "@unq.gbl.spaces")


def _split_channel_thread_id(channel_id: str) -> tuple[str, str]:
    """Split a Teams conversation id into ``(base_id, thread_root_id)``.

    Channel *thread* conversation ids embed the thread root message id as a
    ``;messageid=ROOT`` suffix — the format Bot Framework itself uses on
    inbound channel activities (``conversation.id``) and the format surfaced
    to agents as ``channel_thread_id`` in message context.  Returns the bare
    channel id and the embedded root id (``""`` when absent).

    The root stops at the next ``;`` delimiter: the extracted value is later
    interpolated back into a threaded conversation id, so trailing
    ``;``-delimited segments (e.g. ``;messageid=123;other=1``) must never
    leak into it.
    """
    base, sep, suffix = (channel_id or "").partition(";")
    if sep and suffix.lower().startswith("messageid="):
        return base, suffix.split("=", 1)[1].split(";", 1)[0]
    return channel_id or "", ""


def _is_team_channel_conversation(conversation_id: str) -> bool:
    """Return ``True`` when *conversation_id* is a real Team channel.

    Only Team channels render full-width Adaptive Cards edge-to-edge; chats
    (group / 1:1 / meeting) cap card width to the message bubble, so a
    display card there is better delivered as full-width Markdown text.
    """
    cid = (conversation_id or "").lower()
    # Channel *thread* ids carry a trailing ";messageid=ROOT" (see the
    # thread_key usage in channels/teams.py); strip any ";"-delimited suffix
    # before matching so a channel thread isn't misclassified as a chat and
    # needlessly downgraded to Markdown.
    base = cid.split(";", 1)[0]
    return base.endswith(_TEAMS_CHANNEL_CONVERSATION_SUFFIXES)


def _card_is_interactive(card: Any) -> bool:
    """Return ``True`` when the card contains genuinely interactive elements.

    Interactive cards (data-submitting buttons, inputs, expandable
    sub-cards, visibility toggles) MUST stay Adaptive Cards because Markdown
    text cannot represent them.  Pure display cards (text, images, facts,
    columns, ``Action.OpenUrl`` links) can be rendered as full-width
    Markdown.
    """
    if not isinstance(card, dict):
        return False

    def _walk(node: Any) -> bool:
        if isinstance(node, dict):
            ntype = node.get("type", "")
            if isinstance(ntype, str) and (
                ntype.startswith("Input.") or ntype in _INTERACTIVE_ACTION_TYPES
            ):
                return True
            return any(_walk(value) for value in node.values())
        if isinstance(node, list):
            return any(_walk(item) for item in node)
        return False

    return _walk(card)


def _card_must_stay_card(card: Any) -> bool:
    """Return ``True`` when the card must be delivered as a native card.

    Two independent reasons force native rendering:

    1. **Interactive elements** (inputs, submit buttons, expandable
       sub-cards) — Markdown cannot express them.
    2. **Native-only display elements** (``Chart.*``, ``Media``,
       ``CodeBlock``, ``Table``, ``Carousel``) — downgrading such a card to
       Markdown would silently drop its main content (the chart/table is
       simply skipped by :func:`_card_to_markdown`).

    Cards failing both checks are pure text/image/fact cards that can be
    downgraded to full-width Markdown on chat surfaces.
    """
    if not isinstance(card, dict):
        return False
    if _card_is_interactive(card):
        return True

    def _walk(node: Any) -> bool:
        if isinstance(node, dict):
            ntype = node.get("type", "")
            if isinstance(ntype, str) and (
                ntype.startswith(_NATIVE_ONLY_ELEMENT_PREFIXES)
                or ntype in _NATIVE_ONLY_ELEMENT_TYPES
            ):
                return True
            return any(_walk(value) for value in node.values())
        if isinstance(node, list):
            return any(_walk(item) for item in node)
        return False

    return _walk(card)


def _is_chat_conversation(conversation_id: str) -> bool:
    """Return ``True`` when *conversation_id* is a chat surface.

    Covers group chats and meeting chats (``19:...@thread.v2``, incl.
    ``19:meeting_...@thread.v2``) and 1:1 chats (``@unq.gbl.spaces``).
    Chat surfaces are addressed directly by conversation id — they have no
    team/channel routing and are not governed by channel whitelists.
    """
    cid = (conversation_id or "").lower()
    base = cid.split(";", 1)[0]
    return base.endswith(_TEAMS_CHAT_CONVERSATION_SUFFIXES)


def _card_to_markdown(card: Any) -> str:
    """Render a display-only Adaptive Card to Markdown text.

    Best-effort conversion covering the element types agents actually emit
    (TextBlock, RichTextBlock, Image, FactSet, Container / ColumnSet /
    Column, ActionSet + ``Action.OpenUrl``); unknown elements are skipped.
    Returns ``""`` when nothing renderable is found so callers can fall back
    to sending the card as-is.

    The output is delivered as a Bot Framework text message, which Teams
    renders at full conversation width on every surface — restoring the
    edge-to-edge look that bubble-constrained cards lose in chats.
    """
    if not isinstance(card, dict):
        return ""

    lines: list[str] = []

    def _text_block(node: dict) -> str:
        # Best-effort renderer: a non-string ``text`` (e.g. None on a malformed
        # card) must become empty, never the literal "None" in a user message.
        raw = node.get("text")
        text = raw.strip() if isinstance(raw, str) else ""
        if not text:
            return ""
        size = str(node.get("size", "")).lower()
        weight = str(node.get("weight", "")).lower()
        # Large headline text → Markdown heading; other bold text → **bold**.
        if size in ("extralarge", "large"):
            return f"## {text}"
        if size == "medium" and weight == "bolder":
            return f"### {text}"
        if weight == "bolder":
            return f"**{text}**"
        return text

    def _render(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _render(item)
            return
        if not isinstance(node, dict):
            return
        ntype = node.get("type", "")
        if ntype == "TextBlock":
            rendered = _text_block(node)
            if rendered:
                lines.append(rendered)
        elif ntype == "RichTextBlock":
            parts: list[str] = []
            # Defensive: a malformed card may give a non-list ``inlines`` (e.g.
            # a dict); iterating that would leak its keys into the output.
            inlines = node.get("inlines")
            for inline in inlines if isinstance(inlines, list) else []:
                if isinstance(inline, str):
                    parts.append(inline)
                elif isinstance(inline, dict):
                    # Only real string runs; a non-string ``text`` (e.g. None)
                    # must not stringify into "None" in the downgraded message.
                    inline_text = inline.get("text")
                    if isinstance(inline_text, str):
                        parts.append(inline_text)
            text = "".join(parts).strip()
            if text:
                lines.append(text)
        elif ntype == "Image":
            # Defensive: only render a string url, and only use a string
            # altText (else fall back to "image") so a malformed card with
            # url/altText set to a non-string never leaks the literal "None".
            url = node.get("url")
            if isinstance(url, str) and url:
                alt = node.get("altText")
                alt = alt if isinstance(alt, str) and alt else "image"
                lines.append(f"![{alt}]({url})")
        elif ntype == "FactSet":
            # Defensive: a malformed card may give a non-list ``facts`` (e.g. a
            # dict); iterating that would emit garbage bullets from its keys.
            facts = node.get("facts")
            for fact in facts if isinstance(facts, list) else []:
                if not isinstance(fact, dict):
                    continue
                # Coerce non-string title/value to empty (mirrors _text_block)
                # so a malformed fact (e.g. an explicit None) never renders the
                # literal "None" into the downgraded Markdown.
                raw_title = fact.get("title")
                raw_value = fact.get("value")
                title = raw_title.strip() if isinstance(raw_title, str) else ""
                value = raw_value.strip() if isinstance(raw_value, str) else ""
                # Only wrap the title in bold when it is present; a value-only
                # fact must not emit a malformed empty bold marker (``- **** v``).
                if title and value:
                    lines.append(f"- **{title}** {value}")
                elif title:
                    lines.append(f"- **{title}**")
                elif value:
                    lines.append(f"- {value}")
        elif ntype in ("Container", "ColumnSet", "Column", "ActionSet"):
            _render(node.get("items", []))
            _render(node.get("columns", []))
            _render(node.get("actions", []))
        elif ntype == "Action.OpenUrl":
            # Defensive: only render a string url, and only use a string title
            # (falling back to the url) so a malformed card with a non-string
            # title/url never leaks the literal "None" into the link text.
            url = node.get("url")
            if isinstance(url, str) and url:
                raw_title = node.get("title")
                title = raw_title.strip() if isinstance(raw_title, str) else ""
                lines.append(f"[{title or url}]({url})")

    _render(card.get("body", []))
    _render(card.get("actions", []))

    return "\n\n".join(line for line in lines if line).strip()


def _activity_id_note(activity_id: str) -> str:
    """Confirmation-message suffix for a Bot Framework send.

    Bot Framework can accept an activity on a 2xx response with an empty
    body or a body without an ``id`` (HTTP 202 asynchronous delivery — the
    same semantics ``TeamsChannel._post_activity`` handles for channel
    replies), so a missing activity id after a successful request is still
    a success.  The confirmation just cannot reference an id — saying so
    explicitly avoids the misleading ``(activity ID: )`` phrasing.
    """
    if activity_id:
        return f" (activity ID: {activity_id})"
    return " (accepted by Bot Framework; no activity ID returned)"


#: The ``decision`` argument of the card send tools (human-gates.md I.2).
_DECISION_PROPERTY: dict[str, Any] = {
    "type": "string",
    "default": "",
    "description": (
        "A decision to post instead of card: its id prefixed by its source, "
        "e.g. 'run:' or 'work_item:' then the run or item UUID. Sends the "
        "platform's decision card, answerable in place by its recipient."
        " Never write a decision card's JSON by hand."
    ),
}


def _build_card_activity(
    card: dict[str, Any],
    fallback_text: str = "",
) -> dict[str, Any]:
    """Build a Bot Framework activity with an Adaptive Card attachment.

    The fallback is intentionally carried on the activity's ``summary`` field
    rather than ``text``.  The Teams/Skype connector turns an activity that has
    BOTH renderable ``text`` and ``attachments`` into two separate Skype-level
    messages and rejects it with HTTP 400
    ``BadSyntax: "Activity resulted into multiple skype activities"``.
    ``summary`` is notification-preview metadata only — it never produces a
    second Skype activity — so it preserves a useful fallback/toast preview
    while keeping the activity a single message.

    The card is opted into full-width rendering via ``_ensure_full_width_card``
    so it spans the conversation instead of Teams' constrained default width.
    """
    activity: dict[str, Any] = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": _ensure_full_width_card(card),
            }
        ],
    }
    if fallback_text:
        activity["summary"] = fallback_text
    return activity


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Teams Bot channel tools.

    *token* has the form ``tenant_id||client_id||client_secret``.

    For backwards compatibility, the old ``client_id||client_secret``
    format is also accepted (tenant_id defaults to empty → falls back to
    the legacy ``botframework.com`` tenant endpoint).
    """
    tenant_id, client_id, client_secret = parse_compound_token(token, 3)
    if not client_secret:
        # Legacy format: client_id||client_secret (no tenant)
        client_id, client_secret = tenant_id, client_id
        tenant_id = ""
    if not client_id or not client_secret:
        logger.warning(
            "teams_bot token format invalid — expected tenant_id||client_id||client_secret"
        )
        return []

    # Publish channel whitelist — when set, restricts channel writes to these
    # channels.  Each entry has an "id" field in the format
    # "team_id:channel_id".  Read tools use ``read_channels`` when provided
    # (the blocked-publish case passes the agent's own selection there while
    # ``publish_channels`` holds a never-matching sentinel — see
    # ``build_channel_tools``); otherwise reads fall back to the publish
    # whitelist, preserving the historical single-whitelist behavior.
    _publish_channels: list[dict] | None = _kwargs.get("publish_channels")
    _read_channels: list[dict] | None = (
        _kwargs.get("read_channels") or _publish_channels
    )
    _allowed_ids: set[str] | None = (
        {ch["id"] for ch in _publish_channels if ch.get("id")}
        if _publish_channels
        else None
    )
    _read_allowed_ids: set[str] | None = (
        {ch["id"] for ch in _read_channels if ch.get("id")}
        if _read_channels
        else None
    )

    def _is_channel_allowed(team_id: str, channel_id: str) -> bool:
        """Check if a team:channel pair is in the publish whitelist."""
        return is_publish_allowed(f"{team_id}:{channel_id}", _allowed_ids)

    def _is_channel_readable(team_id: str, channel_id: str) -> bool:
        """Check if a team:channel pair is in the read whitelist."""
        return is_publish_allowed(f"{team_id}:{channel_id}", _read_allowed_ids)

    # Two token caches: Graph API for reads, Bot Framework for channel writes.
    _cached_graph: dict[str, str] = {}
    _cached_bf: dict[str, str] = {}

    async def _get_graph_token() -> str | None:
        """Acquire a Graph API token (for reading teams, channels, messages)."""
        if "token" not in _cached_graph:
            tok = await _acquire_token(
                tenant_id,
                client_id,
                client_secret,
                scope="https://graph.microsoft.com/.default",
            )
            if tok:
                _cached_graph["token"] = tok
        return _cached_graph.get("token")

    async def _get_bf_token() -> str | None:
        """Acquire a Bot Framework token (for sending channel messages)."""
        if "token" not in _cached_bf:
            tok = await _acquire_token(
                tenant_id,
                client_id,
                client_secret,
                scope="https://api.botframework.com/.default",
            )
            if tok:
                _cached_bf["token"] = tok
        return _cached_bf.get("token")

    def _headers(access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    # ── Shared chat helpers ───────────────────────────────────────────────
    #
    # Split of responsibilities for chat (non-channel) surfaces:
    #   - Microsoft Graph (app-only) for READS: user resolution, listing a
    #     user's chats (``/users/{email}/chats`` — the bare ``/chats``
    #     endpoint is delegated-only), reading chat messages.
    #   - Bot Framework for WRITES: Graph does NOT support sending chat
    #     messages with application-only tokens (only the
    #     ``Teamwork.Migrate.All`` import mode) — no permission grant can
    #     change that. Proactive sends therefore go through Bot Framework,
    #     where authorization is roster membership (1:1: the Teams app is
    #     installed for the user; group chat: a member added the bot app to
    #     the conversation).

    async def _resolve_user_id(
        client: httpx.AsyncClient,
        graph_headers: dict[str, str],
        user_email: str,
    ) -> tuple[str | None, str | None]:
        """Resolve a user's AAD object id by email. Returns ``(id, error)``.

        *user_email* is LLM-controlled tool input embedded in the URL path,
        so it is validated against path-breaking characters first.
        """
        if invalid := _validate_path_segment(user_email, "user email"):
            return None, invalid
        user_resp, err = await safe_request(
            client,
            "get",
            f"{GRAPH_API}/users/{user_email}?$select=id,displayName",
            _PROVIDER,
            f"resolve user {user_email}",
            headers=graph_headers,
        )
        if err:
            return None, err
        user_id = user_resp.json().get("id")
        if not user_id:
            return None, f"Error: Could not resolve user ID for {user_email}."
        return user_id, None

    async def _post_conversation_activity(
        client: httpx.AsyncClient,
        bf_token: str,
        service_url: str,
        conversation_id: str,
        activity: dict[str, Any],
        action: str,
    ) -> tuple[str, str | None]:
        """POST an activity into an existing conversation the bot is part of.

        Returns ``(activity_id, error)``.  Fails with a Bot Framework
        roster error when the bot app has not been added to the
        conversation (``BotNotInConversationRoster``).

        ``safe_request`` already turns every non-2xx status into an error,
        so a 2xx response with an empty / non-JSON body or no ``id`` is a
        genuine success (Bot Framework's HTTP 202 asynchronous-acceptance
        mode) — it is reported as         ``("", None)`` and the caller phrases the
        confirmation via :func:`_activity_id_note`.

        *conversation_id* is LLM-controlled tool input embedded in the URL
        path, so it is validated against path-breaking characters first.
        """
        if invalid := _validate_path_segment(conversation_id, "conversation ID"):
            return "", invalid
        resp, err = await safe_request(
            client,
            "post",
            f"{service_url}/v3/conversations/{conversation_id}/activities",
            _PROVIDER,
            action,
            headers=_headers(bf_token),
            json=activity,
        )
        if err:
            return "", err
        try:
            return resp.json().get("id") or "", None
        except ValueError:
            return "", None

    async def _put_conversation_activity(
        client: httpx.AsyncClient,
        bf_token: str,
        service_url: str,
        conversation_id: str,
        activity_id: str,
        activity: dict[str, Any],
        action: str,
    ) -> tuple[str, str | None]:
        """PUT (edit) an activity the bot previously posted.

        Returns ``(activity_id, error)``.  The bot can only edit its own
        messages.  *conversation_id* and *activity_id* are LLM-controlled
        tool input embedded in the URL path, so both are validated against
        path-breaking characters first.
        """
        if invalid := _validate_path_segment(conversation_id, "conversation ID"):
            return "", invalid
        if invalid := _validate_path_segment(activity_id, "activity ID"):
            return "", invalid
        resp, err = await safe_request(
            client,
            "put",
            f"{service_url}/v3/conversations/{conversation_id}/activities/{activity_id}",
            _PROVIDER,
            action,
            headers=_headers(bf_token),
            json=activity,
        )
        if err:
            return "", err
        try:
            return resp.json().get("id") or activity_id, None
        except ValueError:
            return activity_id, None

    async def _create_personal_conversation(
        client: httpx.AsyncClient,
        bf_token: str,
        service_url: str,
        user_id: str,
        activity: dict[str, Any],
        action: str,
    ) -> tuple[str, str | None]:
        """Create (or reuse) a proactive 1:1 conversation and send *activity*.

        Bot Framework ``POST /v3/conversations`` with a single member is the
        supported proactive-message pattern for personal chats.  Requires
        the Teams app to be installed for the target user (personally or
        org-wide); otherwise Bot Framework rejects the request.
        Returns ``(activity_id, error)`` — as with
        :func:`_post_conversation_activity`, a 2xx response without a usable
        body is a success (202 asynchronous acceptance) reported as
        ``("", None)``.
        """
        bf_body: dict[str, Any] = {
            "isGroup": False,
            "bot": {"id": client_id},
            "members": [{"id": user_id}],
            "activity": activity,
        }
        if tenant_id:
            bf_body["tenantId"] = tenant_id
        resp, err = await safe_request(
            client,
            "post",
            f"{service_url}/v3/conversations",
            _PROVIDER,
            action,
            headers=_headers(bf_token),
            json=bf_body,
        )
        if err:
            return "", err
        try:
            data = resp.json()
        except ValueError:
            return "", None
        return data.get("activityId") or data.get("id", ""), None

    # ── Read tools (Graph API) ────────────────────────────────────────────

    async def list_teams_bot_teams_and_channels() -> str:
        """List all teams and their channels visible to the bot."""
        # When a channel whitelist is configured, return it directly
        # without calling the Graph API — avoids needing Group.Read.All and
        # is faster since the admin already curated the channel list.  Uses
        # the read scope so the blocked-publish sentinel never leaks into
        # the listing.
        if _read_channels:
            lines: list[str] = ["Allowed channels (from configuration):"]
            # Group by team_id (first part of "team_id:channel_id")
            teams: dict[str, list[dict]] = {}
            for ch in _read_channels:
                ch_id = ch.get("id", "")
                parts = ch_id.split(":", 1)
                t_id = parts[0] if len(parts) == 2 else "unknown"
                teams.setdefault(t_id, []).append(ch)
            for t_id, chs in teams.items():
                lines.append(f"\n## Team (ID: {t_id})")
                for ch in chs:
                    ch_id_full = ch.get("id", "")
                    ch_channel_id = (
                        ch_id_full.split(":", 1)[1] if ":" in ch_id_full else ch_id_full
                    )
                    ch_name = ch.get("name", "?")
                    lines.append(
                        f"  - #{ch_name} (team_id: {t_id}, channel_id: {ch_channel_id})"
                    )
            return "\n".join(lines)

        access_token = await _get_graph_token()
        if not access_token:
            return "Error: Could not acquire Teams bot token — check integration credentials."

        hdrs = _headers(access_token)
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/teams?$select=id,displayName,description&$top=50",
                _PROVIDER,
                "list teams",
                headers=hdrs,
            )
            if err:
                return err
            teams_list = resp.json().get("value", [])
            if not teams_list:
                return "No teams found. The bot may not have access to any teams."

            lines = []
            for team in teams_list:
                team_id = team["id"]
                team_name = team.get("displayName", "?")

                ch_resp, ch_err = await safe_request(
                    client,
                    "get",
                    f"{GRAPH_API}/teams/{team_id}/channels?$select=id,displayName,description",
                    _PROVIDER,
                    "list channels",
                    headers=hdrs,
                )
                if not ch_err:
                    channels = ch_resp.json().get("value", [])
                    if not channels:
                        continue
                    lines.append(f"\n## {team_name} (ID: {team_id})")
                    for ch in channels:
                        desc = (ch.get("description") or "")[:60]
                        lines.append(
                            f"  - #{ch.get('displayName', '?')} (ID: {ch['id']}) {desc}"
                        )
                else:
                    lines.append(f"\n## {team_name} (ID: {team_id})")
                    lines.append(f"  (Could not list channels: {ch_err})")

            return "\n".join(lines)

    async def read_teams_bot_channel_messages(
        team_id: str, channel_id: str, count: int = 15
    ) -> str:
        """Read recent messages from a Teams channel."""
        if not _is_channel_readable(team_id, channel_id):
            return f"Access denied: channel {team_id}:{channel_id} is not in the allowed channels list for this channel bot."
        access_token = await _get_graph_token()
        if not access_token:
            return "Error: Could not acquire Teams bot token."

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}/messages?$top={count}",
                _PROVIDER,
                "read channel messages",
                headers=_headers(access_token),
            )
            if err:
                return err
            messages = resp.json().get("value", [])
            if not messages:
                return "No messages found in this channel."

            lines: list[str] = []
            for msg in messages:
                sender = ((msg.get("from") or {}).get("user") or {}).get(
                    "displayName", "Unknown"
                )
                app_name = ((msg.get("from") or {}).get("application") or {}).get(
                    "displayName", ""
                )
                display = app_name or sender
                is_bot = bool(app_name)
                created = msg.get("createdDateTime", "")[:19]
                body = (msg.get("body", {}).get("content") or "")[:300]
                msg_id = msg.get("id", "")
                bot_tag = " [BOT]" if is_bot else ""
                lines.append(
                    f"[{created}] {display}{bot_tag} (msg_id: {msg_id}): {body}"
                    f"{_format_graph_attachments(msg)}"
                )
            return "\n".join(lines)

    async def read_teams_bot_thread_replies(
        team_id: str, channel_id: str, message_id: str, count: int = 25
    ) -> str:
        """Read replies in a Teams channel message thread.

        Returns the root message followed by its replies, including sender,
        timestamp, message ID, and content.  The ``msg_id`` values returned
        here can be used as the ``activity_id`` parameter for
        ``delete_teams_bot_channel_message``.
        """
        if not _is_channel_readable(team_id, channel_id):
            return f"Access denied: channel {team_id}:{channel_id} is not in the allowed channels list for this channel bot."
        access_token = await _get_graph_token()
        if not access_token:
            return "Error: Could not acquire Teams bot token."

        lines: list[str] = []
        async with egress_client(timeout=30) as client:
            root_resp, root_err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}/messages/{message_id}",
                _PROVIDER,
                "read thread root message",
                headers=_headers(access_token),
            )
            if not root_err:
                root = root_resp.json()
                sender = ((root.get("from") or {}).get("user") or {}).get(
                    "displayName", "Unknown"
                )
                app_name = ((root.get("from") or {}).get("application") or {}).get(
                    "displayName", ""
                )
                display = app_name or sender
                is_bot = bool(app_name)
                created = root.get("createdDateTime", "")[:19]
                body = (root.get("body", {}).get("content") or "")[:300]
                msg_id = root.get("id", "")
                bot_tag = " [BOT]" if is_bot else ""
                lines.append(
                    f"[ROOT] [{created}] {display}{bot_tag} (msg_id: {msg_id}): {body}"
                    f"{_format_graph_attachments(root)}"
                )

            resp, err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}"
                f"/messages/{message_id}/replies?$top={count}",
                _PROVIDER,
                "read thread replies",
                headers=_headers(access_token),
            )
            if err:
                if lines:
                    return "\n".join(lines) + f"\n(Could not load replies: {err})"
                return err

            replies = resp.json().get("value", [])
            for msg in reversed(replies):
                sender = ((msg.get("from") or {}).get("user") or {}).get(
                    "displayName", "Unknown"
                )
                app_name = ((msg.get("from") or {}).get("application") or {}).get(
                    "displayName", ""
                )
                display = app_name or sender
                is_bot = bool(app_name)
                created = msg.get("createdDateTime", "")[:19]
                body = (msg.get("body", {}).get("content") or "")[:300]
                msg_id = msg.get("id", "")
                bot_tag = " [BOT]" if is_bot else ""
                lines.append(
                    f"[{created}] {display}{bot_tag} (msg_id: {msg_id}): {body}"
                    f"{_format_graph_attachments(msg)}"
                )

        if not lines:
            return "No messages found in this thread."
        return "\n".join(lines)

    # ── Channel write tools (Bot Framework) ───────────────────────────────
    #
    # The Graph API endpoint POST /teams/{id}/channels/{id}/messages does
    # NOT support application permissions — ChannelMessage.Send only exists
    # as a delegated (user) permission.  So we use the Bot Framework
    # "create conversation" endpoint to send proactive channel messages.

    async def send_teams_bot_channel_message(
        team_id: str,
        channel_id: str,
        message: str,
        content_type: str = "text",
        reply_to_id: str = "",
    ) -> str:
        """Send a message to a Teams channel as the bot (via Bot Framework).

        When *reply_to_id* is provided the message is sent as a reply to that
        message thread.  Otherwise a new top-level message is created.

        *channel_id* may be a thread conversation id carrying a
        ``;messageid=ROOT`` suffix (the ``channel_thread_id`` format from
        message context) — the embedded root then threads the send when no
        explicit *reply_to_id* is given.

        A chat-surface conversation id (group chat, meeting chat, 1:1 —
        ``@thread.v2`` / ``@unq.gbl.spaces``) is transparently routed to the
        group-chat send path: chats have no team/channel routing and are not
        governed by channel whitelists.
        """
        channel_id, embedded_root = _split_channel_thread_id(channel_id)
        if embedded_root and not reply_to_id:
            reply_to_id = embedded_root
        if _is_chat_conversation(channel_id):
            return await send_teams_bot_group_chat_message(
                channel_id, message, content_type=content_type
            )
        if not _is_channel_allowed(team_id, channel_id):
            return f"Access denied: channel {team_id}:{channel_id} is not in the publish channels list for this channel bot."
        bf_token = await _get_bf_token()
        if not bf_token:
            return "Error: Could not acquire Bot Framework token — check integration credentials."

        service_url = await _get_service_url(tenant_id)

        # Bot Framework renders the ``text`` field as Markdown by default, but
        # not headings or tables. Normalize agent Markdown so headings downgrade
        # to bold and GFM tables become record lines. ``content_type="html"``
        # keeps the explicit HTML path (Bot Framework ``textFormat="xml"``).
        activity: dict[str, Any] = {"type": "message", "text": message}
        if content_type == "html":
            # ``content_type`` / ``message`` are agent-controlled tool input, so
            # sanitize the HTML (strip scripts / event handlers / unsafe URL
            # schemes) before delivering it with ``textFormat="xml"`` - the same
            # nh3 allow-list the Graph path applies via build_graph_chat_body.
            activity["text"] = markdown_to_teams_html(message, assume_html=True)
            activity["textFormat"] = "xml"
        else:
            activity["text"] = markdown_to_teams_text(message)
            activity["textFormat"] = "markdown"

        if reply_to_id:
            if invalid := _validate_thread_conversation_ids(channel_id, reply_to_id):
                return invalid
            # Teams IGNORES ``replyToId`` for threading (botframework-sdk
            # issue #6626 — the activity lands as a new top-level post).  The
            # documented way to reply into an existing channel thread is to
            # post to the *threaded conversation id*: the channel id with a
            # ``;messageid=ROOT`` suffix.  ``replyToId`` is kept on the
            # activity as harmless metadata.
            activity["replyToId"] = reply_to_id
            thread_conversation = f"{channel_id};messageid={reply_to_id}"
            async with egress_client(timeout=30) as client:
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{service_url}/v3/conversations/{thread_conversation}/activities",
                    _PROVIDER,
                    "reply to channel message",
                    headers=_headers(bf_token),
                    json=activity,
                )
                if err:
                    return err
                reply_id = resp.json().get("id", "")
                return f"Reply sent (activity ID: {reply_id})."
        else:
            body: dict[str, Any] = {
                "isGroup": True,
                "channelData": {"channel": {"id": channel_id}},
                "activity": activity,
            }
            if tenant_id:
                body["tenantId"] = tenant_id

            async with egress_client(timeout=30) as client:
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{service_url}/v3/conversations",
                    _PROVIDER,
                    "send channel message",
                    headers=_headers(bf_token),
                    json=body,
                )
                if err:
                    return err
                data = resp.json()
                activity_id = data.get("activityId") or data.get("id", "")
                # A top-level channel post: the id the Teams poller reads it by.
                await record_teams_channel_post(team_id, channel_id, {"id": activity_id})
                return f"Message sent to channel (activity ID: {activity_id})."

    # ── Adaptive Card helpers ───────────────────────────────────────────

    async def _card_or_decision(
        card: dict[str, Any] | None, decision: str, fallback_text: str
    ) -> tuple[dict[str, Any], str] | str:
        """The card to send: the rendered *decision* when given, else *card*.

        Agents never write a decision card by hand (human-gates.md I.2): the
        decision id renders the same card the notify paths send, answerable
        in place. The answer is the error text when neither resolves.
        """
        if decision and decision.strip():
            from app.services.decision_card_delivery import decision_card_for_tool

            rendered = await decision_card_for_tool(decision.strip())
            if isinstance(rendered, str):
                return rendered
            decision_card, preview = rendered
            return decision_card, fallback_text or preview
        if not isinstance(card, dict) or not card:
            return "Error: pass a card (Adaptive Card JSON) or a decision id."
        return card, fallback_text

    async def send_teams_bot_channel_card(
        team_id: str,
        channel_id: str,
        card: dict[str, Any] | None = None,
        fallback_text: str = "",
        reply_to_id: str = "",
        decision: str = "",
    ) -> str:
        """Send an Adaptive Card to a Teams channel as the bot (via Bot Framework).

        When *reply_to_id* is provided the card is sent as a reply to that
        message thread.  Otherwise a new top-level card message is created.

        The card parameter must be a valid Adaptive Card JSON object
        (see https://adaptivecards.io).  Example::

            {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.5",
                "body": [
                    {"type": "TextBlock", "text": "Hello!", "size": "Large", "weight": "Bolder"},
                    {"type": "TextBlock", "text": "This is a rich card.", "wrap": true}
                ],
                "actions": [
                    {"type": "Action.OpenUrl", "title": "Learn more", "url": "https://filigran.io"}
                ]
            }

        *channel_id* may be a thread conversation id carrying a
        ``;messageid=ROOT`` suffix (the ``channel_thread_id`` format from
        message context) — the embedded root then threads the send when no
        explicit *reply_to_id* is given.

        *decision* (``run:<id>``, ``flow_run:<id>``, ``work_item:<id>``)
        sends that decision's card instead of *card*.

        A chat-surface conversation id (group chat, meeting chat, 1:1 —
        ``@thread.v2`` / ``@unq.gbl.spaces``) is transparently routed to the
        group-chat card path: chats have no team/channel routing and are not
        governed by channel whitelists.
        """
        resolved = await _card_or_decision(card, decision, fallback_text)
        if isinstance(resolved, str):
            return resolved
        card, fallback_text = resolved
        channel_id, embedded_root = _split_channel_thread_id(channel_id)
        if embedded_root and not reply_to_id:
            reply_to_id = embedded_root
        if _is_chat_conversation(channel_id):
            return await send_teams_bot_group_chat_card(
                channel_id, card, fallback_text
            )
        if not _is_channel_allowed(team_id, channel_id):
            return f"Access denied: channel {team_id}:{channel_id} is not in the publish channels list for this channel bot."

        # Adaptive Cards only render full-width in real Team channels. When the
        # target conversation is a chat surface (group/1:1/meeting), Teams caps
        # the card to the message-bubble width — so deliver a display-only card
        # there as full-width Markdown text instead. Cards that Markdown cannot
        # represent (inputs/submit buttons, charts, tables, media) keep their
        # card form.
        if not _is_team_channel_conversation(channel_id) and not _card_must_stay_card(
            card
        ):
            markdown = _card_to_markdown(card)
            if markdown:
                return await send_teams_bot_channel_message(
                    team_id,
                    channel_id,
                    markdown,
                    reply_to_id=reply_to_id,
                )

        bf_token = await _get_bf_token()
        if not bf_token:
            return "Error: Could not acquire Bot Framework token — check integration credentials."

        service_url = await _get_service_url(tenant_id)
        activity = _build_card_activity(card, fallback_text)

        if reply_to_id:
            if invalid := _validate_thread_conversation_ids(channel_id, reply_to_id):
                return invalid
            # Teams IGNORES ``replyToId`` for threading (botframework-sdk
            # issue #6626) — post to the threaded conversation id instead
            # (channel id + ``;messageid=ROOT``), mirroring the text path.
            activity["replyToId"] = reply_to_id
            thread_conversation = f"{channel_id};messageid={reply_to_id}"
            async with egress_client(timeout=30) as client:
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{service_url}/v3/conversations/{thread_conversation}/activities",
                    _PROVIDER,
                    "reply channel card",
                    headers=_headers(bf_token),
                    json=activity,
                )
                if err:
                    return err
                reply_id = resp.json().get("id", "")
                return f"Adaptive Card reply sent (activity ID: {reply_id})."
        else:
            body: dict[str, Any] = {
                "isGroup": True,
                "channelData": {"channel": {"id": channel_id}},
                "activity": activity,
            }
            if tenant_id:
                body["tenantId"] = tenant_id

            async with egress_client(timeout=30) as client:
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{service_url}/v3/conversations",
                    _PROVIDER,
                    "send channel card",
                    headers=_headers(bf_token),
                    json=body,
                )
                if err:
                    return err
                data = resp.json()
                activity_id = data.get("activityId") or data.get("id", "")
                await record_teams_channel_post(team_id, channel_id, {"id": activity_id})
                return f"Adaptive Card sent to channel (activity ID: {activity_id})."

    async def send_teams_bot_chat_card(
        user_email: str,
        card: dict[str, Any] | None = None,
        fallback_text: str = "",
        decision: str = "",
    ) -> str:
        """Send an Adaptive Card as a 1:1 chat message to a user by email.

        Graph resolves the user id; the send goes through Bot Framework
        proactive conversation creation.  The previous implementation also
        created a Graph chat (requiring an ``Application.Read.All`` service
        principal lookup) and fell back to a Graph message send — both dead
        ends with app-only tokens, since Graph forbids application-context
        chat message sending entirely.

        *decision* sends that decision's card instead of *card*.
        """
        resolved = await _card_or_decision(card, decision, fallback_text)
        if isinstance(resolved, str):
            return resolved
        card, fallback_text = resolved
        bf_token = await _get_bf_token()
        if not bf_token:
            return "Error: Could not acquire Bot Framework token."

        access_token = await _get_graph_token()
        if not access_token:
            return "Error: Could not acquire Teams bot token."

        async with egress_client(timeout=30) as client:
            user_id, err = await _resolve_user_id(
                client, _headers(access_token), user_email
            )
            if err:
                return err

            service_url = await _get_service_url(tenant_id)
            # 1:1 chats are always a chat surface, where Adaptive Cards are
            # capped to the message-bubble width. Deliver a display-only card
            # as full-width Markdown text; cards Markdown cannot represent
            # (inputs/buttons, charts, tables, media) stay cards.
            chat_markdown = (
                "" if _card_must_stay_card(card) else _card_to_markdown(card)
            )
            if chat_markdown:
                activity: dict[str, Any] = {
                    "type": "message",
                    "text": markdown_to_teams_text(chat_markdown),
                    "textFormat": "markdown",
                }
            else:
                activity = _build_card_activity(card, fallback_text)
            activity["from"] = {"id": client_id}

            activity_id, err = await _create_personal_conversation(
                client,
                bf_token,
                service_url,
                user_id,
                activity,
                "send chat card",
            )
            if err:
                return err
            return (
                f"Adaptive Card sent to {user_email} via Teams chat"
                f"{_activity_id_note(activity_id)}."
            )

    async def send_teams_bot_chat_message(user_email: str, message: str) -> str:
        """Send a proactive 1:1 chat message to a user by email (as the bot).

        Graph resolves the user id; the send goes through Bot Framework
        proactive conversation creation.  The previous implementation
        created a Graph chat and posted the message via Graph — impossible
        with app-only tokens (Graph supports no application permission for
        sending chat messages), so the tool always failed with
        ``Authorization_RequestDenied`` regardless of granted scopes.
        """
        access_token = await _get_graph_token()
        if not access_token:
            return "Error: Could not acquire Teams bot token."
        bf_token = await _get_bf_token()
        if not bf_token:
            return "Error: Could not acquire Bot Framework token."

        async with egress_client(timeout=30) as client:
            user_id, err = await _resolve_user_id(
                client, _headers(access_token), user_email
            )
            if err:
                return err

            service_url = await _get_service_url(tenant_id)
            activity: dict[str, Any] = {
                "type": "message",
                "text": markdown_to_teams_text(message),
                "textFormat": "markdown",
                "from": {"id": client_id},
            }
            activity_id, err = await _create_personal_conversation(
                client,
                bf_token,
                service_url,
                user_id,
                activity,
                "send chat message",
            )
            if err:
                return err
            return (
                f"Message sent to {user_email} via Teams chat"
                f"{_activity_id_note(activity_id)}."
            )

    async def send_teams_bot_group_chat_message(
        chat_id: str, message: str, content_type: str = "text"
    ) -> str:
        """Send a message into a Teams group chat the bot is a member of.

        Group chats have no team/channel id and cannot appear in publish
        whitelists — authorization is roster membership: a participant must
        have added the bot app to the conversation ("+ Add an app" in the
        chat).  Sends to chats without the bot fail with Bot Framework's
        ``BotNotInConversationRoster`` error.

        *content_type* is not exposed on the tool schema — it exists so the
        channel tools can forward ``content_type="html"`` when a chat
        conversation id is transparently routed here, instead of pushing raw
        HTML through the Markdown normalizer.
        """
        bf_token = await _get_bf_token()
        if not bf_token:
            return "Error: Could not acquire Bot Framework token — check integration credentials."

        service_url = await _get_service_url(tenant_id)
        activity: dict[str, Any] = {
            "type": "message",
            "from": {"id": client_id},
        }
        if content_type == "html":
            # Same sanitized-HTML path as the channel tool (strip scripts /
            # event handlers / unsafe URL schemes before textFormat="xml").
            activity["text"] = markdown_to_teams_html(message, assume_html=True)
            activity["textFormat"] = "xml"
        else:
            activity["text"] = markdown_to_teams_text(message)
            activity["textFormat"] = "markdown"
        async with egress_client(timeout=30) as client:
            activity_id, err = await _post_conversation_activity(
                client,
                bf_token,
                service_url,
                chat_id,
                activity,
                "send group chat message",
            )
            if err:
                return err
            return f"Message sent to group chat{_activity_id_note(activity_id)}."

    async def send_teams_bot_group_chat_card(
        chat_id: str,
        card: dict[str, Any] | None = None,
        fallback_text: str = "",
        decision: str = "",
    ) -> str:
        """Send an Adaptive Card into a Teams group/meeting chat the bot is in.

        Works for regular group chats AND meeting chats (both are
        ``19:...@thread.v2`` chat surfaces — a meeting chat is just a group
        chat with a meeting attached).  Teams caps Adaptive Cards to the
        message-bubble width in chats, so plain display cards are downgraded
        to full-width Markdown text; cards Markdown cannot represent
        (inputs/submit buttons, charts, tables, media) stay native cards.
        *decision* sends that decision's card instead of *card*.
        """
        resolved = await _card_or_decision(card, decision, fallback_text)
        if isinstance(resolved, str):
            return resolved
        card, fallback_text = resolved
        # Chat surface: plain display cards downgrade to full-width Markdown.
        if not _card_must_stay_card(card):
            markdown = _card_to_markdown(card)
            if markdown:
                return await send_teams_bot_group_chat_message(chat_id, markdown)

        bf_token = await _get_bf_token()
        if not bf_token:
            return "Error: Could not acquire Bot Framework token — check integration credentials."

        service_url = await _get_service_url(tenant_id)
        activity = _build_card_activity(card, fallback_text)
        activity["from"] = {"id": client_id}
        async with egress_client(timeout=30) as client:
            activity_id, err = await _post_conversation_activity(
                client,
                bf_token,
                service_url,
                chat_id,
                activity,
                "send group chat card",
            )
            if err:
                return err
            return (
                f"Adaptive Card sent to group chat{_activity_id_note(activity_id)}."
            )

    async def read_teams_bot_chat_messages(chat_id: str, count: int = 20) -> str:
        """Read recent messages from a 1:1 or group chat via Graph (app-only)."""
        if invalid := _validate_path_segment(chat_id, "chat ID"):
            return invalid
        count = _clamp_count(count)
        access_token = await _get_graph_token()
        if not access_token:
            return "Error: Could not acquire Teams bot token."

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/chats/{chat_id}/messages?$top={count}",
                _PROVIDER,
                "read chat messages",
                headers=_headers(access_token),
            )
            if err:
                return err
            messages = resp.json().get("value", [])
            if not messages:
                return "No messages found in this chat."

            lines: list[str] = []
            for msg in reversed(messages):
                if msg.get("messageType") not in ("message", None):
                    continue
                sender = ((msg.get("from") or {}).get("user") or {}).get(
                    "displayName", ""
                )
                app_name = ((msg.get("from") or {}).get("application") or {}).get(
                    "displayName", ""
                )
                display = app_name or sender or "Unknown"
                bot_tag = " [BOT]" if app_name else ""
                created = (msg.get("createdDateTime") or "")[:19]
                body = msg.get("body", {}) or {}
                content = body.get("content") or ""
                if body.get("contentType") == "html":
                    content = re.sub(r"<[^>]+>", "", content)
                content = content.strip()[:300]
                attachments_suffix = _format_graph_attachments(msg)
                if not content and not attachments_suffix:
                    continue
                msg_id = msg.get("id", "")
                lines.append(
                    f"[{created}] {display}{bot_tag} (msg_id: {msg_id}): "
                    f"{content}{attachments_suffix}"
                )
            if not lines:
                return "No messages found in this chat."
            return "\n".join(lines)

    # ── File tools (Graph reads/uploads + Bot Framework link delivery) ────

    persist_file_fn = _kwargs.get("persist_file_fn")

    async def download_teams_bot_file(
        file_url: str, filename: str = ""
    ) -> ToolResult | str:
        """Download a SharePoint/OneDrive file shared in Teams, server-side.

        Resolves the file URL through the Graph ``/shares`` API with the
        bot's app-only token (the SharePoint link itself is never fetched
        directly, so the token only ever goes to graph.microsoft.com) and
        persists the bytes as a reusable platform file with a download card.
        """
        cleaned = (file_url or "").strip()
        if not cleaned.lower().startswith("https://"):
            return "Error: provide the full https:// SharePoint/OneDrive file URL."

        access_token = await _get_graph_token()
        if not access_token:
            return "Error: Could not acquire Teams bot token."

        share_token = _graph_share_token(cleaned)
        hdrs = _headers(access_token)
        async with egress_client(timeout=120, follow_redirects=True) as client:
            meta_resp, err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/shares/{share_token}/driveItem"
                "?$select=id,name,size,file,webUrl,@microsoft.graph.downloadUrl",
                _PROVIDER,
                "resolve shared file",
                headers=hdrs,
            )
            if err:
                return err
            meta = meta_resp.json()
            size = meta.get("size") or 0
            if size > _TEAMS_MAX_FILE_BYTES:
                return (
                    f"Error: file is {round(size / 1024 / 1024, 1)} MB, over the "
                    f"{_TEAMS_MAX_FILE_BYTES // 1024 // 1024} MB download limit."
                )
            derived_name = (
                filename.strip() or meta.get("name") or "teams-download"
            )
            mime = (meta.get("file") or {}).get("mimeType") or (
                mimetypes.guess_type(derived_name)[0]
                or "application/octet-stream"
            )

            # Graph replies 302 to a pre-authenticated location;
            # follow_redirects fetches it (httpx drops the bearer header
            # on the cross-origin hop, so the token never leaks).
            content_resp, err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/shares/{share_token}/driveItem/content",
                _PROVIDER,
                "download shared file",
                headers=hdrs,
            )
            if err:
                return err
            content = content_resp.content

        if not content:
            return "Error: the downloaded file is empty."
        # Re-check against the actual byte length — Graph metadata can omit
        # or zero the size field, and the cap must hold regardless.
        if len(content) > _TEAMS_MAX_FILE_BYTES:
            return (
                f"Error: file is {round(len(content) / 1024 / 1024, 1)} MB, over "
                f"the {_TEAMS_MAX_FILE_BYTES // 1024 // 1024} MB download limit."
            )

        file_ref = None
        if persist_file_fn is not None:
            try:
                file_ref = await persist_file_fn(derived_name, content, mime)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Teams file persistence failed: %s", format_exception_message(exc))
                file_ref = None
        if file_ref is None:
            file_ref = FileRef.from_bytes(
                data=content, filename=derived_name, content_type=mime
            )
        answer = (
            f"Downloaded a {round(len(content) / 1024, 1)} KB file from "
            f"Teams/SharePoint as a reusable file. [[FILE:{file_ref.file_id}]]\n\n"
            f"Forward it to another tool with file_id={file_ref.file_id}, and "
            "include the [[FILE:...]] marker verbatim so the user gets a download card.\n\n"
            # The download still hands on the file's links.
            + untrusted_file_facts(
                source="Teams/SharePoint",
                filename=derived_name,
                content_type=mime,
                details=_drive_item_link_lines(meta),
            )
        )
        return ToolResult(tool_call_id="", content=answer, files=[file_ref])

    async def upload_teams_bot_file_to_channel(
        team_id: str,
        channel_id: str,
        file_id: str,
        message: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Upload a platform file into a Teams channel's SharePoint folder.

        Resolves *file_id* (a file attached to or generated in this
        conversation/run) byte-for-byte, uploads it into the channel's
        Files folder via Graph (app-only ``Files.ReadWrite.All``), and
        posts a Bot Framework message linking the uploaded file.
        """
        if not _is_channel_allowed(team_id, channel_id):
            return f"Access denied: channel {team_id}:{channel_id} is not in the publish channels list for this channel bot."
        if file_context is None:
            return (
                "Error: no file context available — files can only be "
                "forwarded from a conversation or assignment run."
            )
        resolved = await file_context.resolve_upload(file_id)
        if resolved is None:
            return (
                f"Error: file '{file_id}' was not found in this conversation. "
                "Use a file_id from an attached or generated file."
            )
        data, upload_name, mime = resolved
        if len(data) > _TEAMS_MAX_FILE_BYTES:
            return (
                f"Error: file is {round(len(data) / 1024 / 1024, 1)} MB, over "
                f"the {_TEAMS_MAX_FILE_BYTES // 1024 // 1024} MB upload limit."
            )

        access_token = await _get_graph_token()
        if not access_token:
            return "Error: Could not acquire Teams bot token."

        hdrs = _headers(access_token)
        async with egress_client(timeout=120) as client:
            folder_resp, err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/teams/{team_id}/channels/{channel_id}/filesFolder",
                _PROVIDER,
                "resolve channel files folder",
                headers=hdrs,
            )
            if err:
                return err
            folder = folder_resp.json()
            drive_id = (folder.get("parentReference") or {}).get("driveId")
            folder_id = folder.get("id")
            if not drive_id or not folder_id:
                return "Error: could not resolve the channel's SharePoint folder."

            # Percent-encode the filename (LLM/user-controlled) — spaces,
            # ``#``, ``%`` or ``?`` would truncate/invalidate the URL, and
            # a ``/`` would inject an extra path segment. ``safe=""`` also
            # encodes ``/`` so the name stays a single path segment.
            upload_resp, err = await safe_request(
                client,
                "put",
                f"{GRAPH_API}/drives/{drive_id}/items/{folder_id}:"
                f"/{_url_quote(upload_name, safe='')}:/content",
                _PROVIDER,
                "upload file to channel folder",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": mime or "application/octet-stream",
                },
                content=data,
            )
            if err:
                return err
            item = upload_resp.json()
            web_url = item.get("webUrl", "")

        # Announce the upload in the channel with a link (Bot Framework).
        # The filename is user/LLM-controlled — sanitize it before embedding
        # as Markdown link text (same contract as resolve_channel_file_markers).
        display_name = sanitize_md_link_text(upload_name)
        link_line = f"[{display_name}]({web_url})" if web_url else display_name
        announcement = f"{message}\n\n{link_line}" if message else link_line
        send_result = await send_teams_bot_channel_message(
            team_id, channel_id, announcement
        )
        # The name, and the web URL that carries it, go in the quarantine block.
        return uploaded_file_answer(
            f"Uploaded file {file_id} ({round(len(data) / 1024, 1)} KB) to the "
            f"channel's Files folder. {send_result}",
            target=_PROVIDER,
            filename=upload_name,
            links=[("Web URL", web_url)],
        )

    async def delete_teams_bot_channel_message(
        team_id: str, channel_id: str, activity_id: str
    ) -> str:
        """Delete a message (activity) from a Teams channel.

        The bot can only delete messages it has sent.  Uses the Bot Framework
        ``DELETE /v3/conversations/{conversationId}/activities/{activityId}``
        endpoint.
        """
        if not _is_channel_allowed(team_id, channel_id):
            return f"Access denied: channel {team_id}:{channel_id} is not in the publish channels list for this channel bot."
        bf_token = await _get_bf_token()
        if not bf_token:
            return "Error: Could not acquire Bot Framework token — check integration credentials."

        service_url = await _get_service_url(tenant_id)

        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{service_url}/v3/conversations/{channel_id}/activities/{activity_id}",
                _PROVIDER,
                "delete channel message",
                headers=_headers(bf_token),
            )
            if err:
                return err
            return f"Message (activity ID: {activity_id}) deleted from channel."

    async def edit_teams_bot_group_chat_message(
        chat_id: str,
        activity_id: str,
        message: str,
        content_type: str = "text",
    ) -> str:
        """Edit a message the bot previously sent in a Teams group/meeting chat.

        Uses Bot Framework ``PUT /v3/conversations/{id}/activities/{id}``.
        The bot can only edit its own messages.  *content_type* exists so
        the channel edit tool can forward ``content_type="html"`` when a
        chat conversation id is routed here.
        """
        if invalid := _validate_path_segment(activity_id, "activity ID"):
            return invalid
        bf_token = await _get_bf_token()
        if not bf_token:
            return "Error: Could not acquire Bot Framework token — check integration credentials."

        service_url = await _get_service_url(tenant_id)
        activity: dict[str, Any] = {"type": "message", "from": {"id": client_id}}
        if content_type == "html":
            activity["text"] = markdown_to_teams_html(message, assume_html=True)
            activity["textFormat"] = "xml"
        else:
            activity["text"] = markdown_to_teams_text(message)
            activity["textFormat"] = "markdown"
        async with egress_client(timeout=30) as client:
            edited_id, err = await _put_conversation_activity(
                client,
                bf_token,
                service_url,
                chat_id,
                activity_id,
                activity,
                "edit group chat message",
            )
            if err:
                return err
            return f"Group chat message edited{_activity_id_note(edited_id)}."

    async def edit_teams_bot_channel_message(
        team_id: str,
        channel_id: str,
        activity_id: str,
        message: str,
        content_type: str = "text",
    ) -> str:
        """Edit a message the bot previously sent in a Teams channel.

        The bot can only edit its own messages.  Uses Bot Framework
        ``PUT /v3/conversations/{conversationId}/activities/{activityId}``.

        A chat-surface conversation id is routed to the group-chat edit
        path (same as send).  A ``;messageid=ROOT`` suffix on
        *channel_id* is preserved so a thread reply is edited in the
        same conversation it was posted to.
        """
        original_id = channel_id
        channel_id, embedded_root = _split_channel_thread_id(channel_id)
        if _is_chat_conversation(channel_id):
            return await edit_teams_bot_group_chat_message(
                original_id, activity_id, message, content_type=content_type
            )
        if not _is_channel_allowed(team_id, channel_id):
            return (
                f"Access denied: channel {team_id}:{channel_id} is not in the "
                "publish channels list for this channel bot."
            )
        if invalid := _validate_path_segment(activity_id, "activity ID"):
            return invalid
        if invalid := _validate_path_segment(channel_id, "channel ID"):
            return invalid
        if embedded_root and (invalid := _validate_path_segment(embedded_root, "thread root ID")):
            return invalid
        bf_token = await _get_bf_token()
        if not bf_token:
            return "Error: Could not acquire Bot Framework token — check integration credentials."

        service_url = await _get_service_url(tenant_id)
        conversation_id = (
            f"{channel_id};messageid={embedded_root}" if embedded_root else channel_id
        )
        activity: dict[str, Any] = {"type": "message"}
        if content_type == "html":
            activity["text"] = markdown_to_teams_html(message, assume_html=True)
            activity["textFormat"] = "xml"
        else:
            activity["text"] = markdown_to_teams_text(message)
            activity["textFormat"] = "markdown"
        async with egress_client(timeout=30) as client:
            edited_id, err = await _put_conversation_activity(
                client,
                bf_token,
                service_url,
                conversation_id,
                activity_id,
                activity,
                "edit channel message",
            )
            if err:
                return err
            return f"Channel message edited{_activity_id_note(edited_id)}."

    async def list_teams_bot_chats(user_email: str, count: int = 20) -> str:
        """List a user's recent 1:1 and group chats via Graph (app-only).

        The bare ``GET /chats`` endpoint is delegated-only — with the bot's
        application-only token Graph rejects it with ``Requested API is not
        supported in application-only context`` no matter which permissions
        are granted.  The user-scoped ``GET /users/{email}/chats`` variant
        IS supported app-only (``Chat.ReadBasic.All`` or higher), so chats
        are listed through the lens of a participant.
        """
        if invalid := _validate_path_segment(user_email, "user email"):
            return invalid
        count = _clamp_count(count)
        access_token = await _get_graph_token()
        if not access_token:
            return "Error: Could not acquire Teams bot token."

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GRAPH_API}/users/{user_email}/chats"
                f"?$top={count}&$select=id,chatType,topic,lastUpdatedDateTime"
                # Plain expand only: Graph rejects a nested $select inside
                # $expand on this endpoint with HTTP 400 ("Unsupported query
                # parameters 'members($select=...)'"); the expanded member
                # objects already carry displayName.
                "&$expand=members",
                _PROVIDER,
                f"list chats for {user_email}",
                headers=_headers(access_token),
            )
            if err:
                return err
            chats = resp.json().get("value", [])
            if not chats:
                return f"No chats found for {user_email}."

            lines: list[str] = []
            for chat in chats:
                chat_type = chat.get("chatType", "?")
                topic = chat.get("topic") or "(no topic)"
                updated = (chat.get("lastUpdatedDateTime") or "")[:19]
                members = [
                    m.get("displayName")
                    for m in chat.get("members", [])
                    if isinstance(m, dict) and m.get("displayName")
                ]
                member_part = f" — members: {', '.join(members)}" if members else ""
                lines.append(
                    f"[{updated}] {chat_type}: {topic}{member_part} (ID: {chat['id']})"
                )
            return "\n".join(lines)

    # ── Register handlers ─────────────────────────────────────────────────

    executor.register(
        "list_teams_bot_teams_and_channels", list_teams_bot_teams_and_channels
    )
    executor.register(
        "read_teams_bot_channel_messages", read_teams_bot_channel_messages
    )
    executor.register("read_teams_bot_thread_replies", read_teams_bot_thread_replies)
    executor.register("send_teams_bot_channel_message", send_teams_bot_channel_message)
    executor.register("send_teams_bot_channel_card", send_teams_bot_channel_card)
    executor.register("send_teams_bot_chat_card", send_teams_bot_chat_card)
    executor.register("send_teams_bot_chat_message", send_teams_bot_chat_message)
    executor.register(
        "send_teams_bot_group_chat_message", send_teams_bot_group_chat_message
    )
    executor.register("send_teams_bot_group_chat_card", send_teams_bot_group_chat_card)
    executor.register("read_teams_bot_chat_messages", read_teams_bot_chat_messages)
    executor.register("list_teams_bot_chats", list_teams_bot_chats)
    executor.register("download_teams_bot_file", download_teams_bot_file)
    executor.register(
        "upload_teams_bot_file_to_channel", upload_teams_bot_file_to_channel
    )
    executor.register(
        "delete_teams_bot_channel_message", delete_teams_bot_channel_message
    )
    executor.register(
        "edit_teams_bot_channel_message", edit_teams_bot_channel_message
    )
    executor.register(
        "edit_teams_bot_group_chat_message", edit_teams_bot_group_chat_message
    )

    return [
        # Read
        ToolDefinition(
            name="list_teams_bot_teams_and_channels",
            description="List all Microsoft Teams teams and their channels visible to the bot. Returns team names, IDs, and channels with descriptions.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="read_teams_bot_channel_messages",
            description=(
                "Read recent top-level messages from a Microsoft Teams channel. "
                "Returns sender, timestamp, message ID (msg_id), and content. "
                "To read replies within a specific thread, use read_teams_bot_thread_replies instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "Team ID"},
                    "channel_id": {"type": "string", "description": "Channel ID"},
                    "count": {
                        "type": "integer",
                        "default": 15,
                        "description": "Max messages to return",
                    },
                },
                "required": ["team_id", "channel_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="read_teams_bot_thread_replies",
            description=(
                "Read replies in a Microsoft Teams channel message thread. "
                "Returns the root message and all replies with sender, timestamp, "
                "message ID (msg_id), and content. Bot messages are tagged with [BOT]. "
                "The msg_id values can be used as the activity_id parameter for "
                "delete_teams_bot_channel_message or edit_teams_bot_channel_message."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "Team ID"},
                    "channel_id": {"type": "string", "description": "Channel ID"},
                    "message_id": {
                        "type": "string",
                        "description": "ID of the root (parent) message of the thread",
                    },
                    "count": {
                        "type": "integer",
                        "default": 25,
                        "description": "Max replies to return",
                    },
                },
                "required": ["team_id", "channel_id", "message_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # Write
        ToolDefinition(
            name="send_teams_bot_channel_message",
            description=(
                "Send a message to a Microsoft Teams channel as the bot. "
                "If the allowed channels are listed in the system prompt, use those IDs directly. "
                "Otherwise use list_teams_bot_teams_and_channels to discover available channels. "
                "To reply to a specific message thread, provide reply_to_id (use "
                "read_teams_bot_channel_messages to find the message ID). "
                "THREADING: when responding inside the CURRENT conversation thread, "
                "the thread root id is already available in the message context — "
                "channel_thread_id ends with ';messageid=ROOT' (pass that root as "
                "reply_to_id, or pass the full channel_thread_id as channel_id). "
                "Without reply_to_id the message is created as a NEW top-level post. "
                "To change a message already sent, use edit_teams_bot_channel_message "
                "with the returned activity ID - do not post a follow-up."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "Team ID"},
                    "channel_id": {
                        "type": "string",
                        "description": (
                            "Channel ID. A thread conversation id ending in "
                            "';messageid=ROOT' (the channel_thread_id from "
                            "message context) is also accepted and threads "
                            "the reply automatically."
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": "Message content to send",
                    },
                    "content_type": {
                        "type": "string",
                        "enum": ["text", "html"],
                        "default": "text",
                        "description": "Content type: 'text' for plain text, 'html' for rich content",
                    },
                    "reply_to_id": {
                        "type": "string",
                        "default": "",
                        "description": "Optional message/activity ID to reply to. When provided, the message is sent as a threaded reply.",
                    },
                },
                "required": ["team_id", "channel_id", "message"],
            },
            # The same message to the same recipients twice is refused.
            send_guard=SendGuard(recipients=("team_id", "channel_id", "reply_to_id"), body=("message",)),
            source=_SOURCE,
            priority=0,
        ),
        # Adaptive Card tools
        ToolDefinition(
            name="send_teams_bot_channel_card",
            description=(
                "Send an Adaptive Card to a Microsoft Teams channel as the bot. "
                "Use this for rich messages with structured layouts, buttons, links, "
                "images, facts, charts, and action buttons. The card must be a valid "
                "Adaptive Card JSON object (schema: https://adaptivecards.io). Always "
                "include type, $schema, version, and body. Teams natively renders "
                "chart elements (Chart.Donut, Chart.Pie, Chart.VerticalBar, "
                "Chart.HorizontalBar, Chart.Line, Chart.Gauge) on ALL surfaces, "
                "including group and meeting chats. Use fallback_text for a short "
                "notification preview (Teams toast/summary) — not a duplicate of the "
                "card body. To reply to a specific message thread, provide "
                "reply_to_id (use read_teams_bot_channel_messages to find the message ID). "
                "THREADING: when responding inside the CURRENT conversation thread, "
                "the thread root id is already available in the message context — "
                "channel_thread_id ends with ';messageid=ROOT' (pass that root as "
                "reply_to_id, or pass the full channel_thread_id as channel_id). "
                "Without reply_to_id the card is created as a NEW top-level post. "
                "CHATS: a group/meeting chat conversation id (19:...@thread.v2) "
                "passed as channel_id is automatically routed to the group-chat "
                "card path — cards CAN be delivered to meeting chats. "
                "WIDTH: Adaptive Cards only render full-width in real Team channels. "
                "In chats (1:1, group, meeting) Teams caps cards to the message-bubble "
                "width. For long-form text-only content (digests, reports, summaries) "
                "sent to a chat, send Markdown via send_teams_bot_channel_message "
                "instead — it renders full-width. (Text-only display cards posted to "
                "a chat conversation id are auto-converted to full-width Markdown; "
                "cards with charts, tables, media, or interactive elements always "
                "stay native cards.)"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "Team ID"},
                    "channel_id": {
                        "type": "string",
                        "description": (
                            "Channel ID. A thread conversation id ending in "
                            "';messageid=ROOT' (the channel_thread_id from "
                            "message context) is also accepted and threads "
                            "the card reply automatically."
                        ),
                    },
                    "card": {
                        "type": "object",
                        "description": (
                            "Adaptive Card JSON body. Must include: "
                            "type='AdaptiveCard', $schema='http://adaptivecards.io/schemas/adaptive-card.json', "
                            "version='1.5', body=[...]. Body elements: TextBlock (text, size, weight, color, wrap), "
                            "Image (url, size, altText), ColumnSet/Column, FactSet (facts: [{title, value}]), "
                            "ActionSet. Actions: Action.OpenUrl (title, url), Action.Submit (title, data)."
                        ),
                    },
                    "fallback_text": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Short plain-text notification preview for the card "
                            "(used as the Teams toast/summary). Do NOT repeat the full "
                            "card content here — Teams renders the Adaptive Card natively."
                        ),
                    },
                    "reply_to_id": {
                        "type": "string",
                        "default": "",
                        "description": "Optional message/activity ID to reply to. When provided, the card is sent as a threaded reply.",
                    },
                    "decision": _DECISION_PROPERTY,
                },
                "required": ["team_id", "channel_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="send_teams_bot_chat_card",
            description=(
                "Send an Adaptive Card as a 1:1 chat message to a user by email. "
                "Creates a new chat if needed. Use for interactive messages sent "
                "directly to a user. NOTE: 1:1 chats cap Adaptive Cards to the "
                "message-bubble width, so text-only display cards are auto-converted "
                "to full-width Markdown text; cards with charts (Chart.*), tables, "
                "media, or interactive elements stay native cards. For long-form "
                "reports or digests, prefer send_teams_bot_chat_message with Markdown."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_email": {
                        "type": "string",
                        "description": "Email address of the recipient",
                    },
                    "card": {
                        "type": "object",
                        "description": "Adaptive Card JSON body (same format as send_teams_bot_channel_card)",
                    },
                    "fallback_text": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Short plain-text notification preview for the card "
                            "(used as the Teams toast/summary, and as the message body "
                            "only if the card cannot be delivered). Keep it brief — do "
                            "NOT repeat the full card content."
                        ),
                    },
                    "decision": _DECISION_PROPERTY,
                },
                "required": ["user_email"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # Chat tools
        ToolDefinition(
            name="send_teams_bot_chat_message",
            description=(
                "Send a proactive 1:1 chat message to a user by email address, "
                "as the bot (via Bot Framework — creates the conversation if "
                "needed). Requires the Teams app to be installed for the target "
                "user (personally or org-wide). For group chats, use "
                "send_teams_bot_group_chat_message with a chat_id instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_email": {
                        "type": "string",
                        "description": "Email address of the recipient",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message text to send (Markdown supported)",
                    },
                },
                "required": ["user_email", "message"],
            },
            source=_SOURCE,
            priority=0,
            # Artifact markers in the message are sent as links (outbound_markers).
            outbound_text=("message",),
        ),
        ToolDefinition(
            name="send_teams_bot_group_chat_message",
            description=(
                "Send a message into a Microsoft Teams group chat or meeting "
                "chat (multi-person conversation, not a team channel) the bot "
                "has been added to. "
                "The chat_id is the conversation ID (e.g. '19:...@thread.v2', "
                "meeting chats look like '19:meeting_...@thread.v2') — "
                "when replying in an ongoing conversation it is provided in the "
                "message context (channel_thread_id); otherwise discover it via "
                "list_teams_bot_chats with a participant's email. The bot must "
                "be a member of the chat (a participant adds it via '+ Add an "
                "app' in Teams); sends to chats without the bot fail with a "
                "roster error. To change a message already sent, use "
                "edit_teams_bot_group_chat_message with the returned activity "
                "ID - do not post a follow-up."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "Group chat conversation ID (e.g. '19:...@thread.v2')",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message text to send (Markdown supported)",
                    },
                },
                "required": ["chat_id", "message"],
            },
            # The same message to the same recipients twice is refused.
            send_guard=SendGuard(recipients=("chat_id",), body=("message",)),
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="send_teams_bot_group_chat_card",
            description=(
                "Send an Adaptive Card into a Microsoft Teams group chat OR "
                "meeting chat the bot has been added to (same chat_id "
                "discovery as send_teams_bot_group_chat_message — a meeting "
                "chat is a group chat, conversation id '19:meeting_...@thread.v2'). "
                "Teams natively renders chart elements (Chart.Donut, Chart.Pie, "
                "Chart.VerticalBar, Chart.HorizontalBar, Chart.Line, Chart.Gauge) "
                "in chats. NOTE: chats cap Adaptive Cards to the message-bubble "
                "width, so text-only display cards are auto-converted to "
                "full-width Markdown text; cards with charts, tables, media, or "
                "interactive elements stay native cards."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "Group chat conversation ID (e.g. '19:...@thread.v2')",
                    },
                    "card": {
                        "type": "object",
                        "description": "Adaptive Card JSON body (same format as send_teams_bot_channel_card)",
                    },
                    "fallback_text": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Short plain-text notification preview for the card "
                            "(Teams toast/summary). Do NOT repeat the card content."
                        ),
                    },
                    "decision": _DECISION_PROPERTY,
                },
                "required": ["chat_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="read_teams_bot_chat_messages",
            description=(
                "Read recent messages from a Microsoft Teams 1:1 or group chat "
                "by chat ID (via Microsoft Graph; requires the Chat.Read.All or "
                "ChatMessage.Read.All application permission). Returns sender, "
                "timestamp, message ID, and content — bot messages are tagged "
                "[BOT]."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "Chat/conversation ID (e.g. '19:...@thread.v2')",
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max messages to return",
                    },
                },
                "required": ["chat_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_teams_bot_chats",
            description=(
                "List a user's recent Microsoft Teams chats (1:1 and group) by "
                "their email. Returns chat type, topic, members, last update, "
                "and the chat ID — usable as chat_id for "
                "send_teams_bot_group_chat_message / read_teams_bot_chat_messages. "
                "Requires the Chat.ReadBasic.All (or higher) application "
                "permission. (Graph offers no app-only way to list ALL chats; "
                "listing is always through a participant's mailbox.)"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_email": {
                        "type": "string",
                        "description": "Email of a chat participant whose chats to list",
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Max chats to return",
                    },
                },
                "required": ["user_email"],
            },
            source=_SOURCE,
            priority=1,
        ),
        # File tools
        ToolDefinition(
            name="download_teams_bot_file",
            # ``filename`` only names the saved copy: a rename is the same download.
            cosmetic_arguments=("filename",),
            workspace_ingest=True,
            description=(
                "Download a file shared in Microsoft Teams (SharePoint/OneDrive "
                "URL from a message attachment) as a reusable platform file. "
                "Returns a [[FILE:id]] download card; forward the file_id to "
                "other tools. Use the attachment URLs surfaced by "
                "read_teams_bot_chat_messages / read_teams_bot_channel_messages, "
                "or any SharePoint file URL the bot's Graph permissions can "
                "reach (requires the Files.Read.All or Files.ReadWrite.All "
                "application permission)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_url": {
                        "type": "string",
                        "description": "SharePoint/OneDrive file URL (https)",
                    },
                    "filename": {
                        "type": "string",
                        "default": "",
                        "description": "Optional filename override",
                    },
                },
                "required": ["file_url"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="upload_teams_bot_file_to_channel",
            description=(
                "Upload a REAL file (attached to or generated in this "
                "conversation/run — pass its file_id) into a Microsoft Teams "
                "channel's Files folder, then post a message linking it. "
                "Requires the Files.ReadWrite.All application permission. "
                "NOTE: group chats have no bot-writable files location — to "
                "share a file in a group chat, include its [[FILE:id]] marker "
                "in your reply instead (it becomes a download link)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "Team ID"},
                    "channel_id": {"type": "string", "description": "Channel ID"},
                    "file_id": {
                        "type": "string",
                        "description": "Platform file id (from an attachment or generated file)",
                    },
                    "message": {
                        "type": "string",
                        "default": "",
                        "description": "Optional message to post with the file link",
                    },
                },
                "required": ["team_id", "channel_id", "file_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # Delete
        ToolDefinition(
            name="delete_teams_bot_channel_message",
            description=(
                "Delete a message sent by the bot from a Microsoft Teams channel. "
                "The bot can only delete its own messages. To find the activity_id: "
                "(1) use the activity_id returned from send_teams_bot_channel_message, or "
                "(2) use read_teams_bot_thread_replies to list thread messages — the msg_id "
                "of bot messages (tagged [BOT]) can be used directly as the activity_id, or "
                "(3) use read_teams_bot_channel_messages to find root message IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "Team ID"},
                    "channel_id": {"type": "string", "description": "Channel ID"},
                    "activity_id": {
                        "type": "string",
                        "description": "Activity ID (or msg_id from read tools) of the message to delete",
                    },
                },
                "required": ["team_id", "channel_id", "activity_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="edit_teams_bot_channel_message",
            description=(
                "Edit a message the bot already sent in a Microsoft Teams "
                "channel. Replaces the body in place - do not post a "
                "follow-up. The bot can only edit its own messages. Use the "
                "activity_id returned by send_teams_bot_channel_message, or "
                "the msg_id of a [BOT] message from "
                "read_teams_bot_thread_replies / "
                "read_teams_bot_channel_messages. A chat-surface "
                "conversation id is routed to "
                "edit_teams_bot_group_chat_message automatically."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "Team ID"},
                    "channel_id": {
                        "type": "string",
                        "description": (
                            "Channel ID. A thread conversation id ending in ';messageid=ROOT' is also accepted."
                        ),
                    },
                    "activity_id": {
                        "type": "string",
                        "description": ("Activity ID (or msg_id from read tools) of the message to edit"),
                    },
                    "message": {
                        "type": "string",
                        "description": "Replacement body (Markdown supported)",
                    },
                    "content_type": {
                        "type": "string",
                        "enum": ["text", "html"],
                        "default": "text",
                        "description": ("Content type: 'text' for Markdown, 'html' for rich content"),
                    },
                },
                "required": ["team_id", "channel_id", "activity_id", "message"],
            },
            # Two edits of one message in a round are one, the last body.
            coalesce=WriteCoalescing(identity=("activity_id", "team_id", "channel_id"), mode="replace"),
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="edit_teams_bot_group_chat_message",
            description=(
                "Edit a message the bot already sent in a Microsoft Teams "
                "group chat or meeting chat. Replaces the body in place - do "
                "not post a follow-up. The bot can only edit its own "
                "messages. Use the activity_id returned by "
                "send_teams_bot_group_chat_message."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": ("Group chat conversation ID (e.g. '19:...@thread.v2')"),
                    },
                    "activity_id": {
                        "type": "string",
                        "description": "Activity ID of the message to edit",
                    },
                    "message": {
                        "type": "string",
                        "description": "Replacement body (Markdown supported)",
                    },
                },
                "required": ["chat_id", "activity_id", "message"],
            },
            # Two edits of one message in a round are one, the last body.
            coalesce=WriteCoalescing(identity=("activity_id", "chat_id"), mode="replace"),
            source=_SOURCE,
            priority=0,
        ),
    ]
