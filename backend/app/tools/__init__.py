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

"""Tool & MCP subsystem for XTM One.

This package provides:
  - ToolDefinition / ToolCall / ToolResult: Shared types used by LLM providers and the agentic loop.
  - MCPManager: Connects to MCP servers (stdio & SSE), discovers tools, and executes them.
  - run_agent_loop: The core agentic cycle (LLM → tool calls → execute → feed results → repeat).
"""
