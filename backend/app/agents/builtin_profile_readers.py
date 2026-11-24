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

"""The organization profile fields each seeded agent reads (#9173, S2, "Used by").

The Organization page names, on each part of the profile, the agents that read
it. A seeded agent reads the profile through ``get_organization_context``; its
persona (``seed_data/agent_persona/<slug>.md``) says which fields it uses. This
table is that statement in data, next to the persona:
``tests/test_organization_context_readers.py`` fails when a seeded persona calls
``get_organization_context`` without an entry here, or when an entry names a
field its persona never mentions, so the page cannot drift from what the agent
is told to do.

A custom agent is not listed: its persona is free text, and the page never
guesses.
"""

from __future__ import annotations

#: Seeded agent slug -> the profile fields (``OrganizationProfile`` names) its persona reads.
BUILTIN_AGENT_PROFILE_FIELDS: dict[str, tuple[str, ...]] = {
    "ctem-threat-response-orchestrator": (
        "sector",
        "regions",
        "crown_jewels",
        "tech_stack",
        "pir_ids",
        "risk_appetite",
    ),
    "ctem-threat-relevance-analyst": ("sector", "regions", "crown_jewels", "tech_stack", "pir_ids"),
    "ctem-risk-quantifier": ("crown_jewels", "risk_appetite", "currency", "fx_rates"),
    "ctem-remediation-coordinator": ("crown_jewels", "tech_stack", "risk_appetite", "ticketing"),
    "ctem-vuln-prioritization-orchestrator": ("ticketing",),
    "ctem-vuln-triage-analyst": ("sector", "regions", "crown_jewels", "tech_stack", "risk_appetite"),
    "ctem-ai-security-orchestrator": (
        "sector",
        "regions",
        "ai_systems",
        "pir_ids",
        "risk_appetite",
        "loaded_hourly_rate",
        "currency",
    ),
    "ctem-ai-threat-analyst": ("ai_systems",),
    "ctem-third-party-risk-orchestrator": ("sector", "crown_jewels", "pir_ids", "risk_appetite", "currency"),
    "opencti-pir-desk-orchestrator": ("sector", "regions", "pir_ids"),
    "opencti-pir-triage-analyst": ("sector", "regions", "pir_ids"),
    "openaev-detection-desk-orchestrator": ("crown_jewels", "tech_stack"),
    "opencrq-risk-desk-orchestrator": ("risk_appetite", "currency", "fx_rates"),
}

#: The words a persona uses for each field (lower case), checked by the test above.
PROFILE_FIELD_WORDS: dict[str, str] = {
    "sector": "sector",
    "regions": "region",
    "crown_jewels": "primary asset",
    "tech_stack": "software",
    "ai_systems": "ai system",
    "pir_ids": "pir",
    "risk_appetite": "risk appetite",
    "currency": "currency",
    "fx_rates": "reference exchange rate",
    "loaded_hourly_rate": "hourly rate",
    "ticketing": "ticketing",
}
