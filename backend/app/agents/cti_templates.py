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

"""CTI agent templates, adapted from Feedly's CTI Prompt Library.

Nine role templates for the agent creation wizard, seeded in XTM One mode
(``platform_targets``). Each prompt of the library is one **manual assignment**
of a role: the user starts it from the agent page and fills its inputs
(``manual_trigger_parameters``) - the source material to analyse and the
prompt's own variables. The organization variables the prompts share (job role,
sector, region, stakeholder teams) live in the persona's "Organization context"
section, which every assignment reads, completed by the Company Context
knowledge base.

The content lives next to the seed data, one directory per template:
``seed_data/cti_templates/<slug>/`` holds ``persona.md``, ``assignments.json``
(name, description, inputs and prompt file of each assignment, in order) and one
markdown file per assignment prompt. Each prompt keeps Feedly's structure and
replaces "paste your report here" by the trigger inputs and the collection
tools of the XTM One integrations (OpenCTI, Feedly, the enrichment sources).

Source: https://feedly.com/ti-essentials (CTI Prompt Library volumes 1 and 2,
"How to Automate Common CTI Workflows", "Accelerating BFSI CTI Workflows"),
shared by Feedly with no licence restriction; attribution in every template
description and in ``seed_data/NOTICE-cti-pack.md``.
"""

from __future__ import annotations

import json
import pathlib

CTI_TEMPLATES_DIR = pathlib.Path(__file__).parent / "seed_data" / "cti_templates"

#: The modes the CTI templates ship in (``seed_matches_platform``).
CTI_PLATFORM_TARGETS = ["xtm_one", "dev"]

_CATEGORY = "threat intelligence"

_ATTRIBUTION = " Adapted from Feedly's CTI Prompt Library."

_PERSONA_REQUIREMENT = {
    "kind": "persona_field",
    "target": "Organization context",
    "instruction": (
        "Fill in the Organization context section of the persona (the lines "
        "marked TO FILL IN: sector, country or region, stakeholder teams): "
        "every assignment tailors its output to it. Binding the Company Context "
        "knowledge base adds detail."
    ),
}

# Shared by every assignment of these templates. Manual runs that read
# integrations and write a product: human-in-the-loop stays on so a write the
# user asked for (an OpenCTI draft) is approved, the result is notified, and the
# collection steps get more than the 300 s default.
_ASSIGNMENT_DEFAULTS = {
    "trigger_type": "manual",
    "trigger_config": {},
    "ai_label": False,
    "notify_on_complete": True,
    "show_in_digest": False,
    "human_in_the_loop": True,
    "use_knowledge_bases": True,
    "timeout_seconds": 900,
}

# Tradecraft every role applies: source ratings, confidence, TLP.
_RIGOR_SKILLS = ["cti-source-assessment", "cti-confidence-levels", "cti-tlp-guide"]

#: Template metadata, in gallery order. The persona and the assignments are read
#: from ``CTI_TEMPLATES_DIR / slug``.
_TEMPLATE_META: list[dict] = [
    {
        "slug": "cti-intrusion-analyst",
        "name": "Intrusion Analyst",
        "description": (
            "Turns incident reports and threat articles into structured intrusion "
            "analysis: Diamond Model (with its business-risk translation), visual "
            "attack flow, and IOC extraction with enrichment through your connected "
            "threat-intelligence integrations." + _ATTRIBUTION
        ),
        "icon": "Diamond",
        "required_integration_types": ["threat_intel"],
        "default_skills": [
            "cti-threat-intel-sources",
            "cti-ioc-enrichment-workflow",
            "cti-indicator-pivoting",
            "cti-ip-investigation",
            "cti-domain-investigation",
            "cti-hash-investigation",
            "cti-url-investigation",
            "cti-malware-analysis",
            "cti-mitre-attack",
            "cti-stix-bundle",
            "cti-ioc-export",
            *_RIGOR_SKILLS,
        ],
    },
    {
        "slug": "cti-adversary-analyst",
        "name": "Adversary Intelligence Analyst",
        "description": (
            "Profiles threat actors and maps their behaviour: actor profiles, actor "
            "to ATT&CK TTP mapping, ATT&CK Navigator layers, MITRE ATLAS mapping of "
            "AI-related activity, and red team emulation plans grounded in OpenCTI "
            "and your intelligence sources." + _ATTRIBUTION
        ),
        "icon": "Target",
        "required_integration_types": ["threat_intel"],
        "default_skills": [
            "cti-threat-actor-profiling",
            "cti-campaign-tracking",
            "cti-mitre-attack",
            "cti-threat-intel-sources",
            "cti-osint-methodology",
            "cti-ach",
            "cti-china-cyber-espionage",
            "cti-russia-cyber-espionage",
            "cti-iran-cyber-espionage",
            "cti-dprk-cyber-espionage",
            "cti-ransomware-ecosystem",
            *_RIGOR_SKILLS,
        ],
    },
    {
        "slug": "cti-detection-engineer",
        "name": "Detection Engineer",
        "description": (
            "Turns threat reports into deployable detections: Microsoft Sentinel KQL "
            "detection opportunities, Splunk SPL conversion, detection validation and "
            "SOC handoff, and Sigma rules retargetable to KQL, SPL, EQL, YARA-L, "
            "LogScale or YARA." + _ATTRIBUTION
        ),
        "icon": "ScanEye",
        "required_integration_types": ["siem", "threat_intel"],
        "default_skills": [
            "cti-sigma-writing",
            "cti-kql-writing",
            "cti-yara-writing",
            "cti-mitre-attack",
            "cti-threat-intel-sources",
            "opencti-detection-rule-converter-skill",
            "remediation-splunk-spl-detection",
            "cti-confidence-levels",
        ],
    },
    {
        "slug": "cti-threat-hunter",
        "name": "Threat Hunter",
        "description": (
            "Builds threat hunts from intelligence: hunt hypotheses with log sources, "
            "hunt lead extraction and prioritization, feasibility and coverage "
            "against your stack, and a complete hunt package for the hunting "
            "team." + _ATTRIBUTION
        ),
        "icon": "Radar",
        "required_integration_types": ["siem", "threat_intel"],
        "default_skills": [
            "cti-mitre-attack",
            "cti-kql-writing",
            "cti-sigma-writing",
            "cti-indicator-pivoting",
            "cti-threat-intel-sources",
            "cti-key-assumptions-check",
            "cti-structured-analytic-techniques",
            "cti-confidence-levels",
        ],
    },
    {
        "slug": "cti-vulnerability-analyst",
        "name": "Vulnerability Intelligence Analyst",
        "description": (
            "Prioritizes vulnerabilities with threat context: vulnerability "
            "assessment tables (CVSS, EPSS, KEV, exploitation), CVE chaining "
            "analysis and stakeholder-ready vulnerability advisories." + _ATTRIBUTION
        ),
        "icon": "Shield",
        "required_integration_types": ["vulnerability_management", "threat_intel"],
        "default_skills": [
            "cti-vulnerability-intelligence",
            "cti-intelligence-writing",
            "cti-likelihood-language",
            *_RIGOR_SKILLS,
        ],
    },
    {
        "slug": "cti-strategic-analyst",
        "name": "Strategic Threat Intelligence Analyst",
        "description": (
            "Produces strategic intelligence for leadership: industry threat reports, "
            "threat trend identification, structured threat assessments, geopolitical "
            "implications of cyberattacks and trade-policy (tariff) "
            "monitoring." + _ATTRIBUTION
        ),
        "icon": "Globe",
        "required_integration_types": ["threat_intel", "news"],
        "default_skills": [
            "cti-threat-assessment",
            "cti-horizon-scanning",
            "cti-writing-assessments",
            "cti-intelligence-writing",
            "cti-likelihood-language",
            "cti-ach",
            "cti-key-assumptions-check",
            "cti-pir-management",
            "cti-china-cyber-espionage",
            "cti-russia-cyber-espionage",
            "cti-iran-cyber-espionage",
            "cti-dprk-cyber-espionage",
            "cti-hacktivism",
            "cti-supply-chain-threats",
            *_RIGOR_SKILLS,
        ],
    },
    {
        "slug": "cti-intelligence-qa",
        "name": "Intelligence Quality Reviewer",
        "description": (
            "Keeps intelligence products honest: a critic audit of a draft against "
            "its sources, Admiralty (NATO) source reliability and credibility "
            "evaluation, and synthesis of stakeholder feedback into "
            "improvements." + _ATTRIBUTION
        ),
        "icon": "Scale",
        "required_integration_types": ["threat_intel"],
        "default_skills": [
            "cti-quality-control",
            "cti-key-assumptions-check",
            "cti-red-team-analysis",
            "cti-likelihood-language",
            "cti-feedback-loops",
            "cti-stakeholder-management",
            *_RIGOR_SKILLS,
        ],
    },
    {
        "slug": "cti-risk-communications",
        "name": "Cyber Risk and Awareness Advisor",
        "description": (
            "Translates threats into risk decisions and communication: structured "
            "mitigation plans tailored to your stack, prioritized control-gap "
            "recommendations, stakeholder awareness briefs and phishing awareness "
            "newsletters." + _ATTRIBUTION
        ),
        "icon": "Megaphone",
        "required_integration_types": ["threat_intel"],
        "default_skills": [
            "cti-threat-assessment",
            "cti-intelligence-writing",
            "cti-stakeholder-management",
            "cti-intelligence-sharing",
            "cti-phishing-social-engineering",
            "cti-maturity-assessment",
            "cti-likelihood-language",
            "cti-tlp-guide",
        ],
    },
    {
        "slug": "cti-financial-services",
        "name": "Financial Services CTI Analyst",
        "description": (
            "The CTI workflow of a bank, financial institution or insurer: threat "
            "data triage and multi-feed consolidation, third-party and fraud-cyber "
            "assessments, cross-functional impact briefs and courses of action, "
            "incident SITREPs, executive briefs, tabletop exercises and resilience "
            "(DORA-style) gap assessments." + _ATTRIBUTION
        ),
        "icon": "Landmark",
        "required_integration_types": ["threat_intel"],
        "default_skills": [
            "cti-carding-financial-fraud",
            "cti-ransomware-ecosystem",
            "cti-infostealers",
            "cti-initial-access-brokers",
            "cti-phishing-social-engineering",
            "cti-supply-chain-threats",
            "cti-darkweb-collection",
            "cti-threat-intel-sources",
            "cti-ioc-enrichment-workflow",
            "cti-intelligence-writing",
            "cti-sops",
            *_RIGOR_SKILLS,
        ],
    },
]


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8").strip() + "\n"


def _assignments(slug: str) -> list[dict]:
    directory = CTI_TEMPLATES_DIR / slug
    entries = json.loads((directory / "assignments.json").read_text(encoding="utf-8"))
    return [
        {
            **_ASSIGNMENT_DEFAULTS,
            "name": entry["name"],
            "description": entry["description"],
            "prompt": _read(directory / entry["file"]),
            "manual_trigger_parameters": entry["manual_trigger_parameters"],
        }
        for entry in entries
    ]


def _build(meta: dict) -> dict:
    slug = meta["slug"]
    return {
        **meta,
        "category": _CATEGORY,
        "platform_targets": CTI_PLATFORM_TARGETS,
        "disable_chat": False,
        "default_output_format": "markdown",
        "required_integrations": [],
        "recommended_mcp": [],
        "must_be_defined": [_PERSONA_REQUIREMENT],
        "default_persona": _read(CTI_TEMPLATES_DIR / slug / "persona.md"),
        "default_assignments": _assignments(slug),
    }


CTI_TEMPLATES: list[dict] = [_build(meta) for meta in _TEMPLATE_META]
