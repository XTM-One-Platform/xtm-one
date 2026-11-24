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

"""Channel bot CRUD — standalone entity for messaging platform bots."""

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_agentic_ai, require_ai_enabled
from app.api.v1.graph_pagination import paginate_graph_collection
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    get_user_channel_bot,
    get_user_group_ids,
    list_scope_admin_bypass,
    resolve_owner_names,
    user_visible_query,
)
from app.api.v1.schemas.channel_bot import (
    ChannelBotCatalogItem,
    ChannelBotResponse,
    CreateChannelBotRequest,
    UpdateChannelBotRequest,
)
from app.api.v1.schemas.integration import (
    ProviderTool,
    SetupGuide,
    SetupGuidePermission,
)
from app.api.v1.schemas.vault_references import attach_vault_failures, vault_status
from app.core.config import settings
from app.core.security import decrypt_json, decrypt_value, encrypt_value
from app.core.utils import format_exception_message
from app.models.channel_bot import ChannelBot
from app.models.user import User
from app.services.audit import log_audit
from app.services.branding import get_cached_branding
from app.services.tool_classification import infer_tool_type
from app.services.vault import VaultPrincipal, reveal_field, reveal_json_field
from app.services.vault.store import principal_from

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

CHANNEL_BOT_CATALOG: dict[str, dict] = {
    "slack_bot": {
        "name": "Slack",
        "fields": ["api_key"],
        "extra_fields": {
            "signing_secret": "Slack signing secret for request verification"
        },
        "description": "Receive and reply to messages in Slack channels and DMs.",
        "webhook_path": "/api/channels/slack/events",
        "setup_guide": {
            "title": "Slack Bot Setup",
            "url": "https://api.slack.com/apps",
            "steps": [
                "Go to api.slack.com/apps > Create New App > From scratch.",
                "Go to 'OAuth & Permissions' and add all Bot Token Scopes listed below.",
                "Go to 'Agents & Assistants' and turn on the 'App Agent' toggle to allow the bot to act as an assistant (enables typing indicators).",
                "Install the app to your workspace. Copy the 'Bot User OAuth Token' (xoxb-...).",
                "Go to 'Basic Information' and copy the 'Signing Secret'.",
                "Go to 'Event Subscriptions', enable events, and set the Request URL to: {base_url}/api/channels/slack/events",
                "Subscribe to bot events: message.im and app_mention. Optionally add message.channels if the bot should respond to ALL messages in channels (not just @mentions) — but do NOT subscribe to both message.channels and app_mention together, as @mentions would trigger duplicate responses.",
                "Invite the bot to the Slack channels where it should respond (type /invite @YourBotName).",
                "Go to 'Interactivity & Shortcuts', turn Interactivity on and set the Request URL to: {base_url}/api/channels/slack/interactions (decision cards are answered from their buttons).",
            ],
            "permissions": [
                {
                    "scope": "app_mentions:read",
                    "description": "View messages that directly mention the bot in conversations it is in",
                },
                {
                    "scope": "assistant:write",
                    "description": "Show typing/thinking indicators in conversations (requires App Agent enabled)",
                },
                {
                    "scope": "channels:history",
                    "description": "View messages and other content in public channels the bot has been added to",
                },
                {
                    "scope": "channels:join",
                    "description": "Join public channels in a workspace",
                },
                {
                    "scope": "channels:read",
                    "description": "View basic information about public channels in a workspace",
                },
                {"scope": "chat:write", "description": "Send messages as the bot"},
                {
                    "scope": "groups:history",
                    "description": "View messages and other content in private channels the bot has been added to",
                },
                {
                    "scope": "groups:read",
                    "description": "View basic information about private channels the bot has been added to",
                },
                {
                    "scope": "im:history",
                    "description": "View messages and other content in direct messages the bot has been added to",
                },
                {
                    "scope": "im:read",
                    "description": "View basic information about direct messages the bot has been added to",
                },
                {
                    "scope": "im:write",
                    "description": "Start direct messages with people",
                },
                {"scope": "users:read", "description": "View people in a workspace"},
                {
                    "scope": "users:read.email",
                    "description": "View email addresses of people in a workspace",
                },
            ],
        },
    },
    "teams_bot": {
        "name": "Microsoft Teams",
        "fields": ["client_id", "client_secret"],
        "extra_fields": {"tenant_id": "Entra ID (Azure AD) Tenant ID"},
        "description": "Chat with agents inside Microsoft Teams.",
        "webhook_path": "/api/channels/teams/messages",
        "setup_guide": {
            "title": "Microsoft Teams Bot Setup",
            "url": "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
            "steps": [
                "In the Azure portal, go to Entra ID > App registrations > New registration. Note the Application (client) ID and Directory (tenant) ID.",
                "Under Certificates & secrets, create a new Client secret and copy the value.",
                "Under API permissions, add the Microsoft Graph **application** permissions listed below. Then click 'Grant admin consent'.",
                "Create an Azure Bot resource from https://portal.azure.com/#view/HubsExtension/BrowseResource/resourceType/Microsoft.BotService%2FbotServices. Use 'Single Tenant' and 'Use existing app registration' with the Application ID from step 1.",
                "In the Azure Bot resource > Settings > Configuration, set the Messaging endpoint to: {base_url}/api/channels/teams/messages",
                "In the Azure Bot resource > Channels, add the Microsoft Teams channel.",
                "Create the Teams app package in the Teams Developer Portal: https://dev.teams.microsoft.com/apps. Start from the example app manifest below: replace `<YOUR_APP_ID>` with the Application ID from step 1, `your-domain.com` with your platform's domain and the `developer` block (`Your Company`, the `example.com` URLs) with your organization's name, website, privacy and terms pages, and keep the RSC permissions listed below (`ChannelMessage.Read.Group`, `ChatMessage.Read.Chat`, `ChannelMember.Read.Group`) under `authorization.permissions.resourceSpecific`.",
                "Package the manifest with two icons, `color.png` (192×192) and `outline.png` (32×32, a white outline on a transparent background), all three at the root of a .zip file (no folder inside it), and install the bot in your Teams tenant via the Teams admin center or by sideloading the app package.",
            ],
            "permissions": [
                {
                    "scope": "Tenant ID",
                    "description": "The Directory (tenant) ID from Entra ID > App registrations > Overview",
                },
                {
                    "scope": "Client ID",
                    "description": "The Application (client) ID from Entra ID > App registrations > Overview",
                },
                {
                    "scope": "Client Secret",
                    "description": "The secret value from Entra ID > App registrations > Certificates & secrets",
                },
                {
                    "scope": "Channel.ReadBasic.All",
                    "description": "Read the names and descriptions of all channels (application permission, requires admin consent)",
                },
                {
                    "scope": "Chat.Create",
                    "description": "Create chats (application permission, requires admin consent)",
                },
                {
                    "scope": "Chat.Read.WhereInstalled",
                    "description": "Read all chat messages for chats where the Teams app is installed (application permission, requires admin consent)",
                },
                {
                    "scope": "Chat.ReadWrite.WhereInstalled",
                    "description": "Read and write all chat messages for chats where the Teams app is installed (application permission, requires admin consent)",
                },
                {
                    "scope": "ChatMember.Read.WhereInstalled",
                    "description": "Read the members of all chats where the Teams app is installed (application permission, requires admin consent)",
                },
                {
                    "scope": "Team.ReadBasic.All",
                    "description": "Get a list of all teams (application permission, requires admin consent)",
                },
                {
                    "scope": "ChannelMessage.Read.Group (RSC)",
                    "description": "Read channel messages in teams where the app is installed (resource-specific consent, declared in Teams app manifest)",
                },
                {
                    "scope": "ChatMessage.Read.Chat (RSC)",
                    "description": "Read chat messages in chats where the app is installed (resource-specific consent, declared in Teams app manifest)",
                },
                {
                    "scope": "ChannelMember.Read.Group (RSC)",
                    "description": "Receive membership events of shared and private channels the app is added to (resource-specific consent, declared in Teams app manifest)",
                },
            ],
            # ``{APP_NAME}`` placeholders are substituted in
            # ``list_channel_bot_catalog`` against the active
            # ``PlatformSettings.platform_name``, so the rendered manifest
            # example matches the current deployment branding (e.g.
            # "XTM One", "Filigran Copilot", or an admin-customised name)
            # instead of permanently advertising a single product.
            "manifest_example": '{\n  "$schema": "https://developer.microsoft.com/json-schemas/teams/v1.29/MicrosoftTeams.schema.json",\n  "manifestVersion": "1.29",\n  "version": "1.0.0",\n  "id": "<YOUR_APP_ID>",\n  "developer": {\n    "name": "Your Company",\n    "websiteUrl": "https://example.com",\n    "privacyUrl": "https://example.com/privacy",\n    "termsOfUseUrl": "https://example.com/terms"\n  },\n  "name": {\n    "short": "My {APP_NAME} Bot",\n    "full": "My {APP_NAME} Bot for Teams"\n  },\n  "description": {\n    "short": "AI assistant bot",\n    "full": "AI assistant bot powered by {APP_NAME}"\n  },\n  "icons": {\n    "outline": "outline.png",\n    "color": "color.png"\n  },\n  "accentColor": "#FFFFFF",\n  "supportsChannelFeatures": "tier1",\n  "bots": [\n    {\n      "botId": "<YOUR_APP_ID>",\n      "scopes": ["personal", "team", "groupChat"],\n      "supportsFiles": true,\n      "isNotificationOnly": false\n    }\n  ],\n  "composeExtensions": [\n    {\n      "botId": "<YOUR_APP_ID>",\n      "messageHandlers": [\n        {\n          "type": "link",\n          "value": {\n            "domains": ["your-domain.com"]\n          }\n        }\n      ]\n    }\n  ],\n  "permissions": ["identity", "messageTeamMembers"],\n  "validDomains": ["your-domain.com"],\n  "webApplicationInfo": {\n    "id": "<YOUR_APP_ID>",\n    "resource": "api://botid-<YOUR_APP_ID>"\n  },\n  "authorization": {\n    "permissions": {\n      "resourceSpecific": [\n        {\n          "name": "ChannelMessage.Read.Group",\n          "type": "Application"\n        },\n        {\n          "name": "ChatMessage.Read.Chat",\n          "type": "Application"\n        },\n        {\n          "name": "ChannelMember.Read.Group",\n          "type": "Application"\n        }\n      ]\n    }\n  }\n}',
        },
    },
    "whatsapp_bot": {
        "name": "WhatsApp",
        "fields": ["api_key"],
        "extra_fields": {
            "phone_number_id": "WhatsApp Business phone number ID",
            "verify_token": "Webhook verification token (you define this)",
            "app_secret": "Meta app secret (enables X-Hub-Signature-256 webhook verification)",
        },
        "description": "Send and receive WhatsApp messages via Meta Cloud API.",
        "webhook_path": "/api/channels/whatsapp/webhook",
        "setup_guide": {
            "title": "WhatsApp Business Cloud API Setup",
            "url": "https://developers.facebook.com/apps/",
            "steps": [
                "Go to developers.facebook.com > My Apps > Create App > Business type.",
                "Add the WhatsApp product to your app.",
                "Go to WhatsApp > API Setup. Copy the Phone Number ID and generate a permanent Access Token.",
                "Go to WhatsApp > Configuration > Webhook. Set the callback URL to: {base_url}/api/channels/whatsapp/webhook",
                "Choose a Verify Token (any string you define) and paste it in both Meta and the field below.",
                "Subscribe to the 'messages' webhook field.",
            ],
            "permissions": [
                {
                    "scope": "whatsapp_business_messaging",
                    "description": "Send and receive WhatsApp messages",
                },
                {
                    "scope": "whatsapp_business_management",
                    "description": "Manage phone numbers and templates",
                },
            ],
        },
    },
    "telegram_bot": {
        "name": "Telegram",
        "fields": ["api_key"],
        "extra_fields": {
            "webhook_secret_token": (
                "Webhook secret token (you define this; pass the same value "
                "as secret_token to setWebhook)"
            ),
        },
        "description": "Send and receive Telegram messages via Bot API.",
        "webhook_path": "/api/channels/telegram/webhook",
        "setup_guide": {
            "title": "Telegram Bot Setup",
            "url": "https://core.telegram.org/bots#botfather",
            "steps": [
                "Open Telegram and message @BotFather.",
                "Send /newbot and follow the prompts to create a new bot.",
                "Copy the bot token (e.g. 123456:ABC-DEF...).",
                "Paste the token below.",
                "Set the webhook by running: curl -X POST 'https://api.telegram.org/bot<YOUR_TOKEN>/setWebhook' -d 'url={base_url}/api/channels/telegram/webhook'",
                "Send a message to your bot to test the connection.",
            ],
            "permissions": [
                {
                    "scope": "Bot Token",
                    "description": "The token provided by @BotFather for your bot",
                },
            ],
        },
    },
    "googlechat_bot": {
        "name": "Google Chat",
        "fields": ["api_key"],
        "extra_fields": {
            "project_number": (
                "Google Cloud project number (enables bearer JWT webhook "
                "verification)"
            ),
        },
        "description": "Receive and reply to messages in Google Chat spaces and DMs.",
        "webhook_path": "/api/channels/googlechat/webhook",
        "setup_guide": {
            "title": "Google Chat Bot Setup",
            "url": "https://console.cloud.google.com/apis/api/chat.googleapis.com",
            "steps": [
                "In Google Cloud Console, enable the Google Chat API.",
                "Go to the Chat API configuration page.",
                "Under 'Configuration', set the App name and description.",
                "Set the connection settings to HTTPS endpoint URL.",
                "Set the URL to: {base_url}/api/channels/googlechat/webhook",
                "Create a service account (IAM → Service Accounts) and download the JSON key.",
                "Paste the full JSON key contents below as the API key.",
            ],
            "permissions": [
                {
                    "scope": "Service Account JSON Key",
                    "description": "The full JSON key file contents for the Chat bot's service account",
                },
            ],
        },
    },
    "discord_bot": {
        "name": "Discord",
        "fields": ["api_key"],
        "extra_fields": {
            "public_key": (
                "Application public key (enables Ed25519 verification on the "
                "interactions endpoint)"
            ),
        },
        "description": "Receive and reply to messages in Discord servers and DMs.",
        "webhook_path": "/api/channels/discord/interactions",
        "setup_guide": {
            "title": "Discord Bot Setup",
            "url": "https://discord.com/developers/applications",
            "steps": [
                "Go to discord.com/developers/applications and create a New Application.",
                "Go to 'Bot' and click 'Reset Token' to generate a new bot token. Copy it.",
                "Under 'Privileged Gateway Intents', enable 'Message Content Intent'.",
                "Go to 'OAuth2' → 'URL Generator'. Select the 'bot' scope.",
                "Select permissions: Send Messages, Read Message History, Add Reactions.",
                "Copy the generated URL and open it to invite the bot to your server.",
                "Paste the bot token below.",
            ],
            "permissions": [
                {
                    "scope": "Send Messages",
                    "description": "Send messages in text channels",
                },
                {
                    "scope": "Read Message History",
                    "description": "Read messages in channels",
                },
                {"scope": "Add Reactions", "description": "React to messages"},
                {
                    "scope": "Message Content Intent",
                    "description": "Read message content (privileged intent)",
                },
            ],
        },
    },
}

_VALID_PROVIDERS = set(CHANNEL_BOT_CATALOG.keys())


def _build_catalog_tools() -> dict[str, list[ProviderTool]]:
    """Build tool metadata from provider tool builders for channel bot providers."""
    from app.api.v1.integration_catalog import build_dummy_token
    from app.services.integration_tools import CHANNEL_BOT_TOOLS
    from app.tools.base import CallableToolExecutor

    result: dict[str, list[ProviderTool]] = {}
    for provider in _VALID_PROVIDERS:
        builder = CHANNEL_BOT_TOOLS.get(provider)
        if not builder:
            continue
        dummy_token = build_dummy_token(provider)
        dummy_exec = CallableToolExecutor()
        try:
            defs = builder(dummy_token, dummy_exec, refresh_fn=lambda: None)
            result[provider] = [
                ProviderTool(
                    name=d.name,
                    description=d.description,
                    tool_type=infer_tool_type(d.name),
                )
                for d in defs
            ]
        except Exception:
            logger.debug(
                "Failed to build catalog tools for provider %s", provider, exc_info=True
            )
    return result


@router.get("/catalog", response_model=list[ChannelBotCatalogItem])
async def list_channel_bot_catalog(
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List available channel bot provider types."""
    # Resolve ``{base_url}`` and ``{APP_NAME}`` placeholders against the
    # current branding (DB > env > mode-default, via ``get_cached_branding``)
    # so the setup-guide copy and the Teams manifest example match the
    # current deployment branding.
    actual_base_url = settings.base_url.rstrip("/")
    app_name = get_cached_branding().name
    # ``manifest_example`` is JSON; an admin-customised ``platform_name``
    # could contain characters that are valid in plain text but invalid
    # inside a JSON string literal (``"`` / ``\`` / control chars).
    # ``json.dumps`` produces a JSON-quoted string; stripping the
    # surrounding quotes yields a fragment that can be substituted into
    # the existing ``"... {APP_NAME} ..."`` JSON literal without
    # breaking the template.
    app_name_json_escaped = json.dumps(app_name)[1:-1]

    provider_tools = _build_catalog_tools()

    items = []
    for key, val in CHANNEL_BOT_CATALOG.items():
        guide_data = val.get("setup_guide")
        guide = None
        if guide_data:
            resolved_steps = [
                step.replace("{base_url}", actual_base_url).replace(
                    "{APP_NAME}", app_name
                )
                for step in guide_data.get("steps", [])
            ]
            manifest_example = guide_data.get("manifest_example")
            if manifest_example:
                manifest_example = manifest_example.replace(
                    "{base_url}", actual_base_url
                ).replace("{APP_NAME}", app_name_json_escaped)
            guide = SetupGuide(
                title=guide_data["title"],
                url=guide_data.get("url"),
                steps=resolved_steps,
                permissions=[
                    SetupGuidePermission(**p) for p in guide_data.get("permissions", [])
                ],
                # Without this line the manifest_example sat in the
                # catalog dict but never reached the UI dialog that
                # offers a copy-pasteable manifest template (the schema
                # has supported the field for a while).
                manifest_example=manifest_example,
                open_source=guide_data.get("open_source"),
            )
        items.append(
            ChannelBotCatalogItem(
                provider=key,
                name=val["name"],
                fields=val["fields"],
                extra_fields=val.get("extra_fields"),
                description=val.get("description"),
                available_tools=provider_tools.get(key, []),
                webhook_path=val.get("webhook_path"),
                setup_guide=guide,
            )
        )
    return items


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=list[ChannelBotResponse])
async def list_channel_bots(
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all channel bots visible to the current user.

    ``scope=all`` lists every bot for an admin (what a detail page links to).
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        user_visible_query(
            ChannelBot, user.id, user_group_ids, is_admin=list_scope_admin_bypass(scope, user)
        )
    )
    bots = result.scalars().all()
    names = await resolve_owner_names(db, bots)
    # A bot's secrets are an administrator's to edit: only they get the readable locations.
    viewer = principal_from(user) if user.is_admin else None
    responses = [_to_response(r, owner_name=names.get(str(r.user_id)), vault_viewer=viewer) for r in bots]
    await attach_vault_failures(responses, ChannelBot.__tablename__)
    return responses


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=ChannelBotResponse, status_code=status.HTTP_201_CREATED)
async def create_channel_bot(
    body: CreateChannelBotRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Create a new channel bot."""
    if body.provider not in _VALID_PROVIDERS:
        raise HTTPException(
            status_code=400, detail=f"Unknown channel bot provider: {body.provider}"
        )

    # Channel bots are always company-managed (platform-wide resources) — admin only.
    if not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Only admins can create channel bots"
        )

    bot = ChannelBot(
        provider=body.provider,
        name=body.name,
        is_company_managed=True,
        # Owner is always set (the creating admin); visibility is driven by
        # is_company_managed, not by a NULL user_id.
        user_id=user.id,
        allow_anonymous=body.allow_anonymous,
        allow_private=body.allow_private,
        only_mention=body.only_mention,
        requires_approval=body.requires_approval,
        status="disconnected",
    )

    # Encrypt credentials
    if body.api_key:
        bot.api_key_enc = encrypt_value(body.api_key)
    if body.client_id:
        bot.client_id_enc = encrypt_value(body.client_id)
    if body.client_secret:
        bot.client_secret_enc = encrypt_value(body.client_secret)
    if body.extra_config:
        bot.extra_config_enc = encrypt_value(json.dumps(body.extra_config))

    # Mark as connected if credentials are provided
    if bot.api_key_enc or bot.client_id_enc:
        bot.status = "connected"

    if body.allowed_receive_channels is not None:
        bot.allowed_receive_channels = body.allowed_receive_channels or None
    if body.allowed_publish_channels is not None:
        bot.allowed_publish_channels = body.allowed_publish_channels or None

    db.add(bot)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="channel_bot",
        entity_id=bot.id,
        entity_name=bot.name,
        summary=f"Created channel bot '{bot.name}' ({bot.provider})",
        details={
            "provider": bot.provider,
            "is_company_managed": bot.is_company_managed,
            "allow_anonymous": bot.allow_anonymous,
            "allow_private": bot.allow_private,
            "only_mention": bot.only_mention,
            "requires_approval": bot.requires_approval,
        },
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return _to_response(bot)


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


@router.put("/{bot_id}", response_model=ChannelBotResponse)
async def update_channel_bot(
    bot_id: str,
    body: UpdateChannelBotRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update a channel bot's credentials or whitelists."""
    bot = await get_user_channel_bot(db, user.id, bot_id, is_admin=user.is_admin)
    # Channel bots are always company-managed — admin only.
    if not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Only admins can update channel bots"
        )

    if body.name is not None:
        bot.name = body.name
    if body.api_key is not None:
        bot.api_key_enc = encrypt_value(body.api_key) if body.api_key else None
    if body.client_id is not None:
        bot.client_id_enc = encrypt_value(body.client_id) if body.client_id else None
    if body.client_secret is not None:
        bot.client_secret_enc = (
            encrypt_value(body.client_secret) if body.client_secret else None
        )
    if body.api_key is not None or body.client_id is not None or body.client_secret is not None:
        # Recompute connection status from credential presence — previously
        # only an api_key update marked the bot connected (a Teams bot given
        # client credentials via update stayed "disconnected" forever, so
        # every adapter/tool gate on status == "connected" ignored it), and
        # clearing the api_key left a credential-less bot "connected".
        bot.status = (
            "connected" if (bot.api_key_enc or bot.client_id_enc) else "disconnected"
        )
    if body.extra_config is not None:
        # Merge posted keys into the existing config instead of replacing
        # the whole dict — the edit form cannot prefill encrypted values,
        # so a partial edit (e.g. changing only a WhatsApp verify_token)
        # previously wiped every other key (phone_number_id, tenant_id, …).
        # An explicit empty-string value removes that key.
        existing = decrypt_json(bot.extra_config_enc) or {}
        merged = {**existing, **body.extra_config}
        merged = {k: v for k, v in merged.items() if v != ""}
        bot.extra_config_enc = encrypt_value(json.dumps(merged)) if merged else None
    if body.allow_anonymous is not None:
        bot.allow_anonymous = body.allow_anonymous
    if body.allow_private is not None:
        bot.allow_private = body.allow_private
    if body.only_mention is not None:
        bot.only_mention = body.only_mention
    if body.requires_approval is not None:
        bot.requires_approval = body.requires_approval
    if body.allowed_receive_channels is not None:
        bot.allowed_receive_channels = body.allowed_receive_channels or None
    if body.allowed_publish_channels is not None:
        bot.allowed_publish_channels = body.allowed_publish_channels or None

    # Channel bots are always company-managed — ignore any toggle attempt.
    # Ensure consistency in case of stale data. The owner is always preserved
    # (visibility is driven by is_company_managed); backfill legacy rows.
    bot.is_company_managed = True
    if bot.user_id is None:
        bot.user_id = user.id

    await db.flush()
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="channel_bot",
        entity_id=bot.id,
        entity_name=bot.name,
        summary=f"Updated channel bot '{bot.name}'",
        details={
            "provider": bot.provider,
            "is_company_managed": bot.is_company_managed,
            "allow_anonymous": bot.allow_anonymous,
            "allow_private": bot.allow_private,
            "only_mention": bot.only_mention,
            "requires_approval": bot.requires_approval,
        },
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return _to_response(bot)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.delete("/{bot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel_bot(
    bot_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete a channel bot.

    Also cleans up agent ``channel_config`` entries that reference this bot
    so no stale routing data remains.
    """
    from sqlalchemy import Text as SAText
    from sqlalchemy import cast as sql_cast
    from sqlalchemy import select as sql_select

    from app.models.agent import Agent

    bot = await get_user_channel_bot(db, user.id, bot_id, is_admin=user.is_admin)
    # Channel bots are always company-managed — admin only.
    if not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Only admins can delete channel bots"
        )

    # Clean up agent channel_config entries referencing this bot. Pre-filter by
    # a text ``contains`` so only agents that actually reference this bot are
    # loaded (the exact per-channel check below stays authoritative).
    bot_id_str = str(bot.id)
    agents_result = await db.execute(
        sql_select(Agent).where(
            Agent.channel_config.isnot(None),
            sql_cast(Agent.channel_config, SAText).contains(bot_id_str),
        )
    )
    for agent in agents_result.scalars().all():
        if not agent.channel_config:
            continue
        new_config = {}
        changed = False
        for ch_type, ch_cfg in agent.channel_config.items():
            if ch_cfg.get("channel_bot_id") == bot_id_str:
                changed = True  # drop this entry
            else:
                new_config[ch_type] = ch_cfg
        if changed:
            agent.channel_config = new_config if new_config else None
            # Re-derive available_channel_types to stay consistent
            types = []
            if agent.available_channel_types is None or "web" in (
                agent.available_channel_types or []
            ):
                types.append("web")
            if agent.channel_config:
                for ch_type, ch_cfg in agent.channel_config.items():
                    if ch_cfg.get("receive_enabled"):
                        types.append(ch_type)
            agent.available_channel_types = types

    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="channel_bot",
        entity_id=bot.id,
        entity_name=bot.name,
        summary=f"Deleted channel bot '{bot.name}'",
        details={
            "provider": bot.provider,
            "is_company_managed": bot.is_company_managed,
        },
        request=request,
    )
    await db.delete(bot)
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@router.get("/{bot_id}/tools")
async def list_channel_bot_tools(
    bot_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List available tools for a channel bot."""
    bot = await get_user_channel_bot(db, user.id, bot_id, is_admin=user.is_admin)

    from app.services.integration_tools import CHANNEL_BOT_TOOLS as _BUILDERS
    from app.tools.base import CallableToolExecutor

    builder = _BUILDERS.get(bot.provider)
    if not builder:
        return {"tools": []}

    compound_token_providers = {"teams_bot", "whatsapp_bot"}
    dummy_token = (
        "https://dummy.example.com||__dummy__"
        if bot.provider in compound_token_providers
        else "__dummy__"
    )
    dummy_exec = CallableToolExecutor()
    try:
        defs = builder(dummy_token, dummy_exec, refresh_fn=lambda: None)
    except Exception:
        logger.debug(
            "Failed to build tools for channel bot %s (%s)",
            bot.id,
            bot.provider,
            exc_info=True,
        )
        return {"tools": []}

    return {
        "tools": [
            {
                "name": d.name,
                "description": d.description,
                "input_schema": d.input_schema,
            }
            for d in defs
        ]
    }


@router.post("/{bot_id}/test-tool")
async def test_channel_bot_tool(
    bot_id: str,
    body: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Test a single channel bot tool by executing it with provided arguments."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can test channel bot tools")
    bot = await get_user_channel_bot(db, user.id, bot_id, is_admin=user.is_admin)

    tool_name: str = body.get("tool_name", "")
    arguments: dict = body.get("arguments", {})

    if not tool_name:
        return {"status": "error", "message": "tool_name is required"}

    if bot.status != "connected":
        return {"status": "error", "message": "Channel bot is not connected"}

    from app.services.integration_tools import BindingBasis, ToolBindings, build_integration_tools
    from app.tools.base import ToolCall

    # Build tools using the bot's credentials via the integration tools system.
    # The build_integration_tools function accepts provider→id bindings and
    # internally resolves credentials. For channel bots, we pass the bot ID.
    bindings = {bot.provider: str(bot.id)}
    try:
        defs, executor = await build_integration_tools(
            db,
            ToolBindings(bindings, basis=BindingBasis.CALLER_CHECKED, reason="the route's channel bot access check"),
            context={"source": "channel_bot_tool_test", "bot": bot.name},
            channel_bot_mode=True,
        )
    except Exception as exc:
        logger.error("Failed to build channel bot tools: %s", format_exception_message(exc), exc_info=True)
        return {
            "status": "error",
            "message": "Failed to build tools. Check the bot connection and configuration.",
        }

    if tool_name not in executor.tool_names:
        available = [d.name for d in defs]
        return {
            "status": "error",
            "message": f"Tool '{tool_name}' not found. Available: {available}",
        }

    call = ToolCall(id="test", name=tool_name, arguments=arguments)
    start = time.monotonic()
    try:
        result = await executor.execute_tool(call)
        duration_ms = int((time.monotonic() - start) * 1000)
        if result.is_error:
            return {
                "status": "error",
                "message": result.content,
                "duration_ms": duration_ms,
            }
        return {"status": "ok", "output": result.content, "duration_ms": duration_ms}
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.error("Channel bot tool execution failed: %s", format_exception_message(exc), exc_info=True)
        return {
            "status": "error",
            "message": "Tool execution failed. Check the bot connection.",
            "duration_ms": duration_ms,
        }


# ---------------------------------------------------------------------------
# List available channels from the messaging platform
# ---------------------------------------------------------------------------


@router.get("/{bot_id}/channels")
async def list_channel_bot_channels(
    bot_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all available channels from the messaging platform.

    Uses the bot's stored credentials to query the platform API and return
    a flat list of ``{id, name, group}`` objects (same shape as
    ``AllowedChannel``).  Supports Slack and Teams bots.  WhatsApp and
    Telegram return empty lists (no channel-listing concept).
    """
    bot = await get_user_channel_bot(db, user.id, bot_id, is_admin=user.is_admin)

    if bot.status != "connected":
        raise HTTPException(status_code=400, detail="Channel bot is not connected")

    try:
        channels = await _list_platform_channels(bot)
    except Exception as exc:
        logger.warning("Failed to list channels for bot %s: %s", bot.name, format_exception_message(exc))
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch channels from {bot.provider}: {exc}",
        ) from exc

    return {"channels": channels}


async def _list_platform_channels(bot: ChannelBot) -> list[dict]:
    """Fetch all channels from the bot's messaging platform.

    Only Slack and Teams support channel listing.  WhatsApp and Telegram
    have no channel-listing concept; Discord and Google Chat listing is
    not implemented — their whitelist pickers fall back to manual ID entry.
    """
    if bot.provider == "slack_bot":
        return await _list_slack_channels(bot)
    if bot.provider == "teams_bot":
        return await _list_teams_channels(bot)
    return []


async def _list_slack_channels(bot: ChannelBot) -> list[dict]:
    """List all Slack channels the bot can see."""
    import httpx

    token = (await reveal_field(bot, "api_key_enc")) or None
    if not token:
        return []

    headers = {"Authorization": f"Bearer {token}"}
    elements: list[dict] = []
    cursor: str | None = None
    channel_types = "public_channel,private_channel"

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params: dict[str, str] = {
                "types": channel_types,
                "exclude_archived": "true",
                "limit": "200",
            }
            if cursor:
                params["cursor"] = cursor
            resp = await client.get(
                "https://slack.com/api/conversations.list",
                headers=headers,
                params=params,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Slack conversations.list returned HTTP %s: %s",
                    resp.status_code,
                    resp.text[:300],
                )
                return elements
            data = resp.json()
            if not data.get("ok"):
                # Fallback: if groups:read scope is missing, retry public only
                if (
                    data.get("error") == "missing_scope"
                    and "private_channel" in channel_types
                ):
                    logger.info(
                        "Slack missing groups:read, falling back to public channels only"
                    )
                    channel_types = "public_channel"
                    cursor = None
                    elements.clear()
                    continue
                logger.warning(
                    "Slack conversations.list ok=false: error=%s",
                    data.get("error", "unknown"),
                )
                return elements
            for ch in data.get("channels", []):
                elements.append(
                    {
                        "id": ch.get("id", ""),
                        "name": ch.get("name", ""),
                        "group": "private" if ch.get("is_private") else "public",
                    }
                )
            next_cursor = data.get("response_metadata", {}).get("next_cursor", "")
            if not next_cursor:
                break
            cursor = next_cursor
    return elements


async def _list_teams_channels(bot: ChannelBot) -> list[dict]:
    """List all Teams channels the bot can see."""
    import httpx

    # Resolve credentials: tenant_id, client_id, client_secret
    client_id = decrypt_value(bot.client_id_enc) if bot.client_id_enc else None
    client_secret = (
        (await reveal_field(bot, "client_secret_enc")) or None
    )
    extra = await reveal_json_field(bot, "extra_config_enc")
    tenant_id = (extra or {}).get("tenant_id", "")

    if not client_id or not client_secret:
        return []

    # Acquire app-level Graph API token via client credentials
    from app.integrations.providers.teams_bot import _token_url

    token_url = _token_url(tenant_id)

    async with httpx.AsyncClient(timeout=30) as client:
        token_resp = await client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        if not token_resp.is_success:
            logger.warning(
                "Failed to acquire Teams bot token: %s %s",
                token_resp.status_code,
                token_resp.text[:300],
            )
            return []
        access_token = token_resp.json().get("access_token")
        if not access_token:
            return []

    from app.integrations.providers.microsoft import GRAPH_API

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    elements: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        # List teams using /teams endpoint (requires Team.ReadBasic.All)
        teams = await paginate_graph_collection(
            client,
            initial_url=f"{GRAPH_API}/teams",
            headers=headers,
            select_fields="id,displayName",
            top=100,
            max_pages=50,
            logger=logger,
            error_prefix="Failed to list Teams",
            truncate_prefix="Teams bot team listing",
        )
        if not teams:
            return []

        # For each team, list channels
        for team in teams:
            team_id = team.get("id", "")
            team_name = team.get("displayName", "Untitled")
            channels = await paginate_graph_collection(
                client,
                initial_url=f"{GRAPH_API}/teams/{team_id}/channels",
                headers=headers,
                select_fields="id,displayName",
                # The /teams/{id}/channels endpoint rejects $top with
                # HTTP 400 — omit it. The helper also
                # self-heals if $top ever leaks back in.
                top=None,
                max_pages=50,
                logger=logger,
                error_prefix=f"Failed to list channels for team {team_name}",
                truncate_prefix=f"Teams bot channel listing for team {team_name}",
            )
            for ch in channels:
                elements.append(
                    {
                        "id": f"{team_id}:{ch.get('id', '')}",
                        "name": ch.get("displayName", "Untitled"),
                        "group": team_name,
                    }
                )

    return elements


def _to_response(
    bot: ChannelBot, *, owner_name: str | None = None, vault_viewer: VaultPrincipal | None = None
) -> ChannelBotResponse:
    return ChannelBotResponse(
        id=str(bot.id),
        user_id=str(bot.user_id) if bot.user_id else None,
        owner_name=owner_name,
        is_company_managed=bot.is_company_managed,
        provider=bot.provider,
        name=bot.name,
        status=bot.status,
        has_api_key=bool(bot.api_key_enc),
        has_client_id=bool(bot.client_id_enc),
        has_client_secret=bool(bot.client_secret_enc),
        has_extra_config=bool(bot.extra_config_enc),
        allow_anonymous=bot.allow_anonymous,
        allow_private=bot.allow_private,
        only_mention=bot.only_mention,
        requires_approval=bot.requires_approval,
        allowed_receive_channels=bot.allowed_receive_channels,
        allowed_publish_channels=bot.allowed_publish_channels,
        vault_secrets=vault_status(bot, vault_viewer),
        created_at=bot.created_at.isoformat() if bot.created_at else "",
        updated_at=bot.updated_at.isoformat() if bot.updated_at else "",
    )
