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

"""Platform insights: the platform's own analytics, objects, releases,
AI adoption, audit trail and holdings, read by an agent.

One built-in suite, ``platform_insights`` (``builtin_tools/platform_insights.py``),
over the read paths the admin pages already use - the trace read store and
``usage_breakdown`` (LLM Analytics), the Admin Objects union
(``api/v1/admin_objects.ENTITY_MAP``), the AI Adoption endpoints, the audit log
- so what an agent says and what the dashboard shows cannot drift apart.

Every tool reads through :func:`scope.load_scope`: an administrator's agent
(``agent_tool_admin_bypass``) reads the platform, anyone else their own slice,
and every result says which one it served (``scope``, ``scope_note``).
"""
