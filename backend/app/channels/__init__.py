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

"""Multi-channel messaging framework.

This package provides a unified abstraction for receiving user messages
and sending XTM One agent responses across multiple platforms:

- **web**         – Real-time chat via Server-Sent Events in the frontend
- **slack**       – Slack Bot (Events API + Web API)
- **teams**       – Microsoft Teams Bot (Bot Framework)
- **whatsapp**    – WhatsApp Business Cloud API (Meta)
- **telegram**    – Telegram Bot API
- **discord**     – Discord Bot (Gateway WebSocket + Interactions endpoint)
- **google_chat** – Google Chat app (HTTP events)
"""

from app.channels.base import (
    BaseChannel,
    ChannelType,
    InboundMessage,
    OutboundMessage,
    get_channel_bot,
)

__all__ = [
    "BaseChannel",
    "ChannelType",
    "InboundMessage",
    "OutboundMessage",
    "get_channel_bot",
]
