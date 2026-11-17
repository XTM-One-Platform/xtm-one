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

"""Agent accounts: an agent operating a real user account of a messaging platform.

- ``events``: the provider-agnostic shapes every provider produces.
- ``rules``: the pure rule engine, the presets, the listening plan, the AI label.
- ``conversation``: the conversation and context an agent turn runs with.
- ``state``: the short-lived conversation state, in Redis.
- ``pipeline``: from a message to an answer, approvals included.
- ``listening``: subscriptions, polls, notifications, the scheduler's step.
- ``providers``: the platform interface and its implementations.

Contract: ``dev-docs/reference/agent-accounts.md``.
"""
