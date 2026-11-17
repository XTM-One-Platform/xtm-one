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

"""CTI skills, adapted from the cti-skills pack by Liberty91 Ltd (MIT License).

Fifty-one built-in skills seeded company-managed in XTM One mode: analytic
tradecraft, investigation workflows, detection writing, intelligence
production and knowledge cells on nation-state and cybercrime threats. The
pack's lookup scripts and API-key setup are replaced by the XTM One
integrations (VirusTotal, Shodan, AbuseIPDB, GreyNoise, urlscan, MISP,
OpenCTI, Ransomware.live, CrowdStrike, MITRE ATT&CK, ...): the twenty-one
lookup and API skills of the pack are one guide, **Threat Intel Sources**.

Bodies live in ``seed_data/skill_content/<slug>.md``; a skill flagged
``resources`` also ships ``seed_data/skill_resources/<slug>/`` (read with
``read_skill_resource``). Licence and upstream in ``seed_data/NOTICE-cti-pack.md``;
the integrations the pack needed and XTM One does not have are listed in
``dev-docs/cti-pack.md``.
"""

from __future__ import annotations

#: The modes the CTI skills ship in (``seed_matches_platform``).
_PLATFORM_TARGETS = ["xtm_one", "dev"]

#: The pack's licence, outside the body the agent reads: the copyright notice
#: is the skill's ``license`` (the Agent Skills frontmatter field, shown in its
#: Catalog tab), and the full MIT text travels with every copy of the skill (an
#: export, a catalog clone) as a bundled ``LICENSE.md`` the catalog prompt does
#: not advertise.
_LICENSE_NOTICE = (
    "MIT License. Copyright (c) 2026 Liberty91 Ltd. Adapted for XTM One from the "
    "cti-skills pack (https://github.com/Liberty91LTD/cti-skills); the full "
    "licence text is bundled with the skill as LICENSE.md."
)
_LICENSE = {"LICENSE.md": "licenses/cti-skills-LICENSE.md"}

CTI_SKILLS: list[dict] = [
    {
        "slug": "cti-orchestrator",
        "name": "CTI Orchestrator",
        "description": (
            "Default entry point for any cyber threat intelligence request that "
            "does not name a specific skill: investigate an IP, domain, hash or "
            "URL, profile a threat actor, track a campaign, write a threat "
            "assessment, enrich an IOC list or write detection rules. Classifies "
            "the request, loads the right investigation, analysis or production "
            "skill by name, checks active PIRs, and applies the rigor rules "
            "(Admiralty source rating, TLP, confidence 0-100, likelihood language) "
            "to the result."
        ),
        "compatibility": "Routes to skills that use the connected threat-intel integrations (VirusTotal, GreyNoise, AbuseIPDB, Shodan, urlscan, OpenCTI, MISP, CrowdStrike, Ransomware.live, abuse.ch and others); OpenCTI PIRs when connected, agent memory otherwise.",
        "content": "cti-orchestrator",
        "tags": ["builtin", "cti", "orchestration"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-hyperloop",
        "name": "CTI Hyperloop Framework",
        "description": (
            "Explains the CTI Hyperloop framework: the intelligence lifecycle run "
            "as a high-tempo loop at strategic, operational and tactical levels at "
            "the same time, with bidirectional feedback. Load it when the user asks "
            "about the Hyperloop, how to organise CTI work across intelligence "
            "levels and lifecycle phases, which roles and skills handle each phase, "
            "or how a request maps to direction, collection, processing, analysis, "
            "dissemination and feedback."
        ),
        "compatibility": None,
        "content": "cti-hyperloop",
        "tags": ["builtin", "cti", "tradecraft", "orchestration"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-threat-intel-sources",
        "name": "Threat Intel Sources",
        "description": (
            "Reference guide to every threat-intelligence source an investigation "
            "can query: which integration answers which question (quick "
            "source-per-indicator matrix), the exact tools for IPs, domains, URLs, "
            "hashes, actors, ransomware victims and CVEs, how to read each result "
            "(verdict fields, false-positive traps, what not found means), default "
            "Admiralty ratings, quota and privacy caveats, and alternatives for "
            "sources without an integration (OTX, Censys, ReversingLabs). Load it "
            "when choosing sources for an enrichment or interpreting a lookup "
            "result."
        ),
        "compatibility": "Uses VirusTotal, Shodan, AbuseIPDB, GreyNoise, urlscan, MISP, OpenCTI, Ransomware.live and CrowdStrike integrations, plus abuse.ch, GTI, Recorded Future, DomainTools, Defender TI, CrowdSec, ANY.RUN, VulnCheck, Feedly and MITRE ATT&CK when connected; reports skipped sources otherwise.",
        "content": "cti-threat-intel-sources",
        "tags": ["builtin", "cti", "collection", "investigation"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-ip-investigation",
        "name": "IP Investigation",
        "description": (
            "Investigates an IPv4 or IPv6 address end-to-end: queries GreyNoise, "
            "VirusTotal, AbuseIPDB, Shodan, OpenCTI, MISP and vendor feeds in "
            "parallel, short-circuits on benign scanner noise, consolidates a "
            "verdict, ranks pivot candidates and returns a rated summary with "
            "Admiralty ratings, TLP and confidence. Load it when the user asks to "
            "investigate, check, enrich or characterize an IP, or asks whether an "
            "IP is malicious."
        ),
        "compatibility": "Uses GreyNoise, VirusTotal, AbuseIPDB, Shodan, OpenCTI, MISP, CrowdStrike, Google Threat Intelligence, Recorded Future, abuse.ch, CrowdSec, Defender TI and DomainTools integrations when connected; skipped sources are reported, never guessed.",
        "content": "cti-ip-investigation",
        "tags": ["builtin", "cti", "investigation"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-domain-investigation",
        "name": "Domain Investigation",
        "description": (
            "Investigates a domain or hostname end-to-end: VirusTotal reputation, "
            "existing urlscan scans, DNS resolution and Shodan hosting fingerprint, "
            "WHOIS / registration age, internal OpenCTI and MISP correlation, and a "
            "Ransomware.live victim-claim sweep, then ranks pivots (resolved IP, "
            "certificate siblings, newly registered clusters) and returns a rated "
            "summary. Load it when the user asks to investigate, check or "
            "characterize a domain or hostname."
        ),
        "compatibility": "Uses VirusTotal, urlscan, Shodan, Ransomware.live, OpenCTI, MISP, CrowdStrike, DomainTools, Google Threat Intelligence, Recorded Future, abuse.ch and Defender TI integrations when connected; skipped sources are reported, never guessed.",
        "content": "cti-domain-investigation",
        "tags": ["builtin", "cti", "investigation"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-hash-investigation",
        "name": "Hash Investigation",
        "description": (
            "Investigates a file hash (MD5, SHA-1, SHA-256): detection stats, "
            "malware family, file details, sandbox behaviour, internal OpenCTI and "
            "MISP correlation, vendor verdicts, and infrastructure pivots (C2 IPs "
            "and domains, parent and dropped files), then decides whether deeper "
            "malware analysis is needed. Load it when the user asks to check a "
            "hash, identify malware from a hash, or enrich a sample."
        ),
        "compatibility": "Uses VirusTotal, abuse.ch (MalwareBazaar, ThreatFox, URLhaus), OpenCTI, MISP, CrowdStrike, Google Threat Intelligence, Recorded Future, ANY.RUN and Defender integrations when connected; skipped sources are reported, never guessed.",
        "content": "cti-hash-investigation",
        "tags": ["builtin", "cti", "investigation"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-url-investigation",
        "name": "URL Investigation",
        "description": (
            "Investigates a URL: live urlscan scan (unlisted by default) with "
            "redirect chain, contacted infrastructure and screenshot, reputation "
            "from VirusTotal, URLhaus, OpenCTI, MISP and vendor feeds, then "
            "extracts the parent domain and resolved IP for follow-up and returns a "
            "rated summary. Load it when the user asks to scan, check or "
            "investigate a URL or a suspicious link, for example from a phishing "
            "e-mail."
        ),
        "compatibility": "Uses urlscan, VirusTotal, abuse.ch, OpenCTI, MISP, CrowdStrike, Google Threat Intelligence, Recorded Future and ANY.RUN integrations when connected; skipped sources are reported, never guessed.",
        "content": "cti-url-investigation",
        "tags": ["builtin", "cti", "investigation"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-indicator-pivoting",
        "name": "Indicator Pivoting",
        "description": (
            "Methodology for expanding one known indicator (IP, domain, URL, file "
            "hash, TLS certificate, registrant, actor or family name) into related "
            "infrastructure across the IOC graph: decision trees by indicator type "
            "with the exact integration tool for each pivot, pivot-quality scoring, "
            "a worked multi-hop example and a pivot-chain documentation template. "
            "Load it when asked \"what else is connected to this IP / domain / hash "
            "/ cert / actor?\", to grow a seed IOC into a campaign cluster, or to "
            "walk past an investigation's first hop."
        ),
        "compatibility": "Uses VirusTotal, Shodan, urlscan, GreyNoise, AbuseIPDB, DomainTools, abuse.ch, GTI, MISP, OpenCTI, Ransomware.live and CrowdStrike integrations when connected; reports skipped sources otherwise.",
        "content": "cti-indicator-pivoting",
        "tags": ["builtin", "cti", "investigation", "pivoting", "tradecraft"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-ioc-enrichment-workflow",
        "name": "IOC Enrichment Workflow",
        "description": (
            "Workflow for enriching a batch of raw IOCs (IPs, domains, URLs, "
            "hashes, emails): parse, refang, validate and deduplicate them, filter "
            "known-benign values, route each type to the right integrations with "
            "batch tools first, and synthesise one scored enrichment record per "
            "indicator with Admiralty rating, confidence and skipped sources. Load "
            "it when the user pastes or uploads a list of indicators to triage, "
            "enrich or prepare before pushing them into OpenCTI, MISP or a SIEM."
        ),
        "compatibility": "Uses VirusTotal, AbuseIPDB, GreyNoise, CrowdSec, Shodan, urlscan, abuse.ch, GTI, Recorded Future, CrowdStrike, DomainTools, MISP, OpenCTI and Ransomware.live integrations when connected, plus code_interpreter; skips and reports missing sources.",
        "content": "cti-ioc-enrichment-workflow",
        "tags": ["builtin", "cti", "investigation", "enrichment", "workflow"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-ioc-export",
        "name": "IOC Export",
        "description": (
            "Export formats and procedures for indicators of compromise: CSV "
            "(column definitions), STIX 2.1, OpenIOC XML and MISP event JSON, with "
            "MISP attribute types, a dedupe/validate/TLP/confidence workflow and "
            "delivery as a downloadable file, an OpenCTI draft, a MISP event or a "
            "detection platform. Load it when asked to export, package, convert or "
            "share a set of IOCs, or to build a blocklist file."
        ),
        "compatibility": "Uses code_interpreter, generate_file and stix_bundle_validator; writes to OpenCTI, MISP, Sentinel, Defender, CrowdStrike or Splunk ES only when asked and connected.",
        "content": "cti-ioc-export",
        "tags": ["builtin", "cti", "production", "export", "ioc"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-stix-bundle",
        "name": "STIX Bundle Authoring",
        "description": (
            "STIX 2.1 authoring reference: bundle structure, templates for "
            "indicator, threat actor, malware, attack pattern, campaign, identity "
            "and relationship objects, pattern syntax per IOC type, common "
            "relationship types, the standard TLP marking-definition IDs and the "
            "MISP reference-only marking rule, plus an authoring checklist. Load it "
            "when writing or reviewing a STIX bundle by hand; for OpenCTI-bound "
            "bundles pair it with the OpenCTI STIX Bundle Construction skill and "
            "always validate with stix_bundle_validator."
        ),
        "compatibility": "Uses stix_bundle_validator, code_interpreter and generate_file; OpenCTI and MISP write-back only when asked and connected.",
        "content": "cti-stix-bundle",
        "tags": ["builtin", "cti", "production", "stix"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-malware-analysis",
        "name": "Malware Analysis",
        "description": (
            "Guidance for characterising a malware sample without executing it: "
            "static analysis (PE headers, suspicious imports, strings, packers), "
            "sandbox interpretation (process, file, registry and network "
            "behaviour), sources by question across sandboxes and threat "
            "intelligence, ATT&CK mapping and an analysis report template. Load it "
            "when asked \"what does this binary do?\", to analyse a hash or attached "
            "sample in depth, or when a hash investigation flags a novel sample."
        ),
        "compatibility": "Uses VirusTotal (reports, behaviour, relationships), ANY.RUN, CrowdStrike Falcon Sandbox and Intel, GTI, abuse.ch MalwareBazaar, MITRE ATT&CK, MISP and OpenCTI integrations when connected; code_interpreter reads attached files as data only.",
        "content": "cti-malware-analysis",
        "tags": ["builtin", "cti", "analysis", "malware"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-mitre-attack",
        "name": "MITRE ATT&CK Reference",
        "description": (
            "How to query MITRE ATT&CK (Enterprise, Mobile, ICS) through the MITRE "
            "ATT&CK integration: find techniques, tactics, groups, software, "
            "campaigns, mitigations and detection strategies, pivot relationships "
            "(group to techniques, technique to groups, mitigations, detections), "
            "key object types, common technique IDs, coverage-gap analysis and "
            "Navigator layers, plus MITRE ATLAS for AI systems. Load it when "
            "mapping behaviour to ATT&CK IDs, comparing an actor with an ATT&CK "
            "group, or assessing detection coverage."
        ),
        "compatibility": "Uses the MITRE ATT&CK integration (and MITRE ATLAS for AI systems); optional SIEM/EDR rule listings and OpenCTI.",
        "content": "cti-mitre-attack",
        "tags": ["builtin", "cti", "knowledge", "mitre-attack"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-ach",
        "name": "Analysis of Competing Hypotheses",
        "description": (
            "Analysis of Competing Hypotheses (ACH): evaluates three or more "
            "competing explanations against all significant evidence in a "
            "consistency matrix, focusing on disproving hypotheses, weighting "
            "evidence by Admiralty rating and testing linchpin evidence. Load it "
            "for ambiguous attribution ('who is behind this campaign?'), multiple "
            "plausible scenarios, contested or high-stakes judgments, or when asked "
            "to run an ACH or weigh competing hypotheses."
        ),
        "compatibility": None,
        "content": "cti-ach",
        "tags": ["builtin", "cti", "analysis", "tradecraft"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-key-assumptions-check",
        "name": "Key Assumptions Check",
        "description": (
            "Key Assumptions Check: surfaces the assumptions behind an analytical "
            "judgment, rates each for importance and confidence, flags "
            "high-importance low-confidence assumptions and states their impact on "
            "the conclusion and the collection needed to validate them. Load it "
            "when asked 'what are we assuming?' or 'are these assumptions still "
            "valid?', before publishing a high-impact assessment, or as the first "
            "step of a major assessment."
        ),
        "compatibility": None,
        "content": "cti-key-assumptions-check",
        "tags": ["builtin", "cti", "analysis", "tradecraft"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-red-team-analysis",
        "name": "Red Team Analysis",
        "description": (
            "Red Team Analysis (devil's advocate): deliberately builds the "
            "strongest case against a prevailing analytical judgment, covering "
            "overweighted and ignored evidence, alternative explanations, "
            "vulnerable assumptions and possible adversary deception, then rates "
            "the challenge as compelling, partially compelling or not compelling. "
            "Load it when asked for the opposing case, to argue the other side, or "
            "to stress-test an assessment before it drives decisions."
        ),
        "compatibility": None,
        "content": "cti-red-team-analysis",
        "tags": ["builtin", "cti", "analysis", "tradecraft"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-structured-analytic-techniques",
        "name": "Structured Analytic Techniques",
        "description": (
            "Index of Structured Analytic Techniques for CTI: diagnostic, "
            "contrarian and imaginative techniques, a decision guide mapping each "
            "analytic question to ACH, Key Assumptions Check, Red Team Analysis, "
            "Threat Assessment, Horizon Scanning or Source Assessment, and how to "
            "combine them. Load it when asked which structured analytic technique "
            "to use, for the list of SATs, or when choosing a method for a hard "
            "analytical question."
        ),
        "compatibility": None,
        "content": "cti-structured-analytic-techniques",
        "tags": ["builtin", "cti", "analysis", "tradecraft"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-horizon-scanning",
        "name": "Horizon Scanning",
        "description": (
            "Horizon Scanning: strategic forecasting of emerging threats over 6-24 "
            "months. Collects weak signals (Feedly emerging trends and trending "
            "threats, OpenCTI, news and RSS, research, underground sources), "
            "categorises them, develops best/worst/most likely scenarios with "
            "likelihood and impact, and defines early warning indicators. Load it "
            "when asked what is coming next, for an emerging threat or strategic "
            "outlook report, or to hunt weak signals before they materialise."
        ),
        "compatibility": "Uses Feedly, OpenCTI, NewsAPI, RSS, Google Search, Vulnerability Intelligence, VulnCheck, Intel 471, Flashpoint, Cybersixgill, SOCRadar, Ransomware.live, GTI, Recorded Future, CrowdStrike and MITRE ATLAS when connected; works without them in analysis-only mode.",
        "content": "cti-horizon-scanning",
        "tags": ["builtin", "cti", "analysis", "collection"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-source-assessment",
        "name": "Source Assessment (Admiralty Scale)",
        "description": (
            "Source Assessment with the NATO Admiralty Scale: rates source "
            "reliability (A-F) and information credibility (1-6) with decision "
            "guides and combined-rating examples, distinguishes independent from "
            "derivative sources, and explains how to record ratings in a product or "
            "in OpenCTI. Load it when asked 'is this reliable?' or to rate a "
            "source, when weighting evidence in an analysis, or before publishing a "
            "product that cites sources."
        ),
        "compatibility": None,
        "content": "cti-source-assessment",
        "tags": ["builtin", "cti", "tradecraft"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-threat-assessment",
        "name": "Threat Assessment",
        "description": (
            "Threat Assessment: formally evaluates the threat an actor or scenario "
            "poses to a target as Intent + Capability + Opportunity, with rating "
            "tables for each component, a combined threat level from CRITICAL to "
            "NEGLIGIBLE, where to find the evidence in OpenCTI and connected "
            "intelligence, exposure and security tools, and an output template. "
            "Load it when asked to assess a threat, rate a threat level, or answer "
            "'how much of a threat is X to us?'."
        ),
        "compatibility": "Uses OpenCTI, MITRE ATT&CK, GTI, CrowdStrike, Intel 471, Recorded Future, Ransomware.live, Vulnerability Intelligence, VulnCheck, OpenAEV and connected SIEM/EDR integrations when connected; works without them in analysis-only mode.",
        "content": "cti-threat-assessment",
        "tags": ["builtin", "cti", "analysis", "production"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-tlp-guide",
        "name": "TLP Guide",
        "description": (
            "Traffic Light Protocol v2.0 guide: definitions of TLP:CLEAR, GREEN, "
            "AMBER, AMBER+STRICT and RED, a decision tree, default TLP per product "
            "type, how to state TLP in products and STIX, and how to apply or "
            "remove TLP markings on OpenCTI objects and MISP events. Load it when "
            "asked 'what TLP should this be?', before sharing a finished product, "
            "or when asked to mark an OpenCTI object with a TLP."
        ),
        "compatibility": "Uses OpenCTI (marking definitions) and MISP (TLP taxonomy tags) when connected and the user asks to mark objects; otherwise analysis-only.",
        "content": "cti-tlp-guide",
        "tags": ["builtin", "cti", "tradecraft", "production"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-confidence-levels",
        "name": "Confidence Levels",
        "description": (
            "Confidence Levels: the 0-100 confidence scale (shared with MISP and "
            "OpenCTI) with five named bands from Very Low to Very High, the three "
            "factors that determine confidence, how to express it in text, tables "
            "and OpenCTI, the difference from likelihood, and when to upgrade or "
            "downgrade. Load it when assigning confidence to a judgment, when asked "
            "'how confident are we?', or before publishing an assessment."
        ),
        "compatibility": None,
        "content": "cti-confidence-levels",
        "tags": ["builtin", "cti", "tradecraft"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-likelihood-language",
        "name": "Likelihood Language",
        "description": (
            "Likelihood Language: the probability yardstick for forward-looking "
            "statements, from remote (under 5%) to almost certain (over 95%), with "
            "correct and incorrect usage, how to combine likelihood with confidence, when "
            "not to use it, and a mapping from informal words like could, may and "
            "might. Load it when phrasing a prediction or risk statement, when "
            "asked 'how likely is X?', or when reviewing a product's probabilistic "
            "language."
        ),
        "compatibility": None,
        "content": "cti-likelihood-language",
        "tags": ["builtin", "cti", "tradecraft", "production"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-quality-control",
        "name": "Intelligence Quality Control",
        "description": (
            "Intelligence Quality Control: peer review checklist for intelligence "
            "products across structure, analytic rigor, sourcing, mandated "
            "confidence and likelihood language, technical accuracy and writing, "
            "with weighted scoring, issue severities, platform spot-checks (ATT&CK, "
            "IOC format, STIX validation, OpenCTI) and a review template. Load it "
            "to review, QA or peer-review a draft report, assessment or IOC package "
            "before it is published or shared."
        ),
        "compatibility": "Uses MITRE ATT&CK, OpenCTI, MISP warninglists, Vulnerability Intelligence and the STIX bundle validator for spot-checks when available; works without them as a checklist review.",
        "content": "cti-quality-control",
        "tags": ["builtin", "cti", "tradecraft", "production"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-intelligence-writing",
        "name": "Intelligence Writing",
        "description": (
            "Conventions and templates for finished intelligence products: BLUF, "
            "active voice, facts versus assessments versus assumptions, mandated "
            "confidence, likelihood, Admiralty and TLP language, and templates for "
            "flash reports, intelligence summaries, threat assessments, threat "
            "actor profiles and campaign reports, with delivery as chat output, a "
            "structured report or a file. Load when asked to write a flash report, "
            "intel summary, briefing, threat assessment or campaign report, or to "
            "apply intelligence writing conventions to a draft."
        ),
        "compatibility": "Delivers through generate_report and generate_file; optionally records Reports and Notes in OpenCTI and verifies techniques with the MITRE ATT&CK integration when connected.",
        "content": "cti-intelligence-writing",
        "tags": ["builtin", "cti", "production"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-writing-assessments",
        "name": "Writing Assessments",
        "description": (
            "Distinct structures for the three assessment types: threat assessment "
            "(intent + capability + opportunity, five threat levels), risk "
            "assessment (threat x vulnerability x impact with a risk matrix) and "
            "vulnerability assessment (exposure + susceptibility - resilience with "
            "P1-P5 patch priorities), plus the general rules for stating judgments. "
            "Load when asked to write a threat, risk or vulnerability assessment, "
            "to rate a threat or risk level, or to choose the right assessment "
            "template."
        ),
        "compatibility": "Draws evidence from OpenCTI, MITRE ATT&CK, CrowdStrike, GTI, Recorded Future, Intel 471 and Vulnerability Intelligence / VulnCheck integrations when connected; works without them in analysis-only mode.",
        "content": "cti-writing-assessments",
        "tags": ["builtin", "cti", "production", "analysis"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-sops",
        "name": "CTI Standard Operating Procedures",
        "description": (
            "Seven CTI standard operating procedures with step-by-step actions "
            "mapped to XTM One tools: daily intelligence triage, IOC processing, "
            "flash report creation with expedited review, threat actor profile "
            "updates, stakeholder briefings, quarterly PIR review and knowledge "
            "cell maintenance. Load when asked how the CTI team should run triage, "
            "process an IOC list, issue a flash report, update an actor profile, "
            "prepare a briefing or review PIRs, or when setting up recurring CTI "
            "assignments."
        ),
        "compatibility": "Uses OpenCTI (PIRs, entities, drafts), MISP, Feedly, RSS, Recorded Future, GTI, Intel 471, Flashpoint, Vulnerability Intelligence and Ransomware.live integrations when connected, plus agent memory; works without them in analysis-only mode.",
        "content": "cti-sops",
        "tags": ["builtin", "cti", "orchestration"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-pir-management",
        "name": "PIR Management",
        "description": (
            "End-to-end lifecycle of Priority Intelligence Requirements: SMART "
            "format, the PIR record template, priority guide, creation, collection "
            "planning, satisfaction tracking, quarterly review and retirement, with "
            "strategic, operational and tactical examples. Maps PIRs onto native "
            "OpenCTI PIRs (weighted targeting criteria, flagged entities ranked by "
            "PIR score) and falls back to agent memory. Load when asked to create, "
            "review, refine or retire PIRs, to check what satisfies a PIR, or to "
            "align collection to the PIR set."
        ),
        "compatibility": "Uses OpenCTI PIR tools (manage_opencti_pir, query_opencti_pir_entities, query_opencti_pir_relationships) when connected; otherwise keeps PIRs in agent memory.",
        "content": "cti-pir-management",
        "tags": ["builtin", "cti", "orchestration", "knowledge"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-feedback-loops",
        "name": "Intelligence Feedback Loops",
        "description": (
            "Closing the intelligence cycle: consumer feedback questions and the "
            "actions they trigger, source quality tracking and Admiralty rating "
            "adjustments, analyst retrospectives, and detection effectiveness "
            "metrics (true and false positive rates, coverage) from SIEM, EDR and "
            "OpenAEV results, all fed back into planning. Load when asked to "
            "collect or analyse stakeholder feedback, run a retrospective, re-rate "
            "a source, or measure how well intelligence-driven detections perform."
        ),
        "compatibility": "Reads detection outcomes from Microsoft Sentinel, Defender, Splunk, Elastic Security, Google SecOps and OpenAEV when connected; stores feedback in agent memory or knowledge bases.",
        "content": "cti-feedback-loops",
        "tags": ["builtin", "cti", "orchestration"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-stakeholder-management",
        "name": "CTI Stakeholder Management",
        "description": (
            "Getting intelligence to the right people in the right format: common "
            "CTI stakeholders with their needs, formats and cadence, a stakeholder "
            "register kept in agent memory or a knowledge base, tailoring rules for "
            "executives, security operations and risk teams, a TLP dissemination "
            "matrix and feedback questions. Load when asked who a product should go "
            "to, how to tailor a product for a given audience, who the CTI "
            "customers are, or to build or update the stakeholder register."
        ),
        "compatibility": None,
        "content": "cti-stakeholder-management",
        "tags": ["builtin", "cti", "orchestration", "production"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-intelligence-sharing",
        "name": "Intelligence Sharing",
        "description": (
            "Sharing intelligence externally under TLP: ISACs, FIRST, MISP events "
            "(warning-list check, attributes, tags, distribution, publish on "
            "request), STIX/TAXII and OpenCTI outbound sharing (TAXII collections, "
            "live streams, CSV feeds), bilateral agreements, what to share and not "
            "share, quality standards and legal considerations. Load when asked "
            "about ISAC participation, MISP communities, TAXII feeds, publishing "
            "intelligence to partners, or how a TLP level governs dissemination."
        ),
        "compatibility": "Uses MISP and OpenCTI (manage_opencti_data_sharing, markings) integrations when connected, plus stix_bundle_validator and generate_file; works without them in advisory mode.",
        "content": "cti-intelligence-sharing",
        "tags": ["builtin", "cti", "production"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-maturity-assessment",
        "name": "CTI Maturity Assessment",
        "description": (
            "Benchmark a CTI programme against a five-level maturity model (ad-hoc "
            "to optimized) across six dimensions: collection, analysis, production, "
            "dissemination, management and tooling, with an interview-led method, "
            "platform evidence, a self-assessment questionnaire and the order in "
            "which to improve. Load when asked how mature the CTI programme is, "
            "what to improve next, or to run or repeat a CTI maturity assessment."
        ),
        "compatibility": "Optionally gathers evidence from OpenCTI (PIRs, connectors, data sharing, playbooks), MISP feeds and SIEM/EDR indicator lists when connected; works as an interview-only assessment without them.",
        "content": "cti-maturity-assessment",
        "tags": ["builtin", "cti", "orchestration"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-osint-methodology",
        "name": "OSINT Methodology",
        "description": (
            "Structured open-source intelligence collection: planning, source "
            "categories, Google, GitHub, certificate transparency and Shodan search "
            "operators, broad sweep then targeted collection, Admiralty rating of "
            "every finding, a finding documentation template and ethical and legal "
            "boundaries. Load when asked to research a threat, actor, campaign or "
            "indicator in open sources, to build a collection plan, or for search "
            "operators and OSINT tradecraft."
        ),
        "compatibility": "Uses search_google_web, web_fetch and browser, plus Feedly, RSS, NewsAPI, DomainTools, Shodan, urlscan, OpenCTI and MISP integrations when connected; works with web tools alone.",
        "content": "cti-osint-methodology",
        "tags": ["builtin", "cti", "collection", "tradecraft"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-threat-actor-profiling",
        "name": "Threat Actor Profiling",
        "description": (
            "Build or update a threat actor profile: a six-step collection workflow "
            "(OpenCTI and MISP first, then CrowdStrike, GTI, Recorded Future, Intel "
            "471, Feedly and Flashpoint, MITRE ATT&CK and Ransomware.live, then "
            "open sources and infrastructure pivoting), the canonical profile "
            "template (attribution, targeting, TTPs, tooling, infrastructure, "
            "campaigns, relationships, gaps, sources), vendor alias mapping and "
            "maintenance rules. Load when asked to profile an actor, tell me about "
            "actor X, or update an intrusion set."
        ),
        "compatibility": "Uses OpenCTI, MISP, CrowdStrike, GTI, Recorded Future, Intel 471, Feedly, Flashpoint, MITRE ATT&CK, Ransomware.live and abuse.ch integrations when connected; works with web search alone in reduced mode.",
        "content": "cti-threat-actor-profiling",
        "tags": ["builtin", "cti", "analysis", "knowledge"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-campaign-tracking",
        "name": "Campaign Tracking",
        "description": (
            "Document a named campaign across time and victims: how a campaign maps "
            "onto OpenCTI (Campaign entity, attribution, targets, techniques, "
            "indicators, reports), collection sources, the campaign record template "
            "(timeline, attribution, victimology, ATT&CK attack chain, Diamond "
            "Model, IOC clusters, TTP evolution, detection guidance, gaps), "
            "campaign linking and the lifecycle from active to historical. Load "
            "when asked to start or update a campaign record or when an "
            "investigation surfaces a multi-incident cluster."
        ),
        "compatibility": "Uses OpenCTI, GTI, CrowdStrike, Recorded Future, Feedly, Flashpoint, Ransomware.live, MITRE ATT&CK, MISP and SIEM/EDR integrations when connected; works without them in analysis-only mode.",
        "content": "cti-campaign-tracking",
        "tags": ["builtin", "cti", "analysis", "knowledge"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-vulnerability-intelligence",
        "name": "Vulnerability Intelligence",
        "description": (
            "Prioritise CVEs beyond CVSS: EPSS, CISA KEV, weaponisation stages and "
            "exposure combined into a P1-P5 patch-now-versus-later matrix, with a "
            "four-step evidence workflow (core CVE data, exploitation and "
            "weaponisation, exposure, decision) and a vulnerability assessment "
            "template. Load when asked whether a CVE is exploited, which "
            "vulnerabilities to patch first, or to rank a list of CVEs."
        ),
        "compatibility": "Uses Vulnerability Intelligence (NVD/EPSS/KEV), VulnCheck, GreyNoise, CrowdSec, Feedly, Flashpoint, Recorded Future, Intel 471, Cybersixgill, GTI, OpenCTI, MISP, Defender, CrowdStrike and Shodan integrations when connected; works with web search alone in reduced mode.",
        "content": "cti-vulnerability-intelligence",
        "tags": ["builtin", "cti", "analysis", "investigation"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-sigma-writing",
        "name": "Sigma Rule Writing",
        "description": (
            "Writes vendor-agnostic Sigma detection rules in YAML: rule structure, "
            "log source categories, field modifiers, condition logic, ATT&CK tags, "
            "quality checklist, and how to test the logic against a connected SIEM. "
            "Load it when the user asks for a Sigma rule (\"write a Sigma rule for "
            "X\"), a behavioural detection for a technique, or when a hash "
            "investigation or malware analysis surfaces behaviour worth detecting "
            "across SIEMs."
        ),
        "compatibility": "Uses MITRE ATT&CK, Recorded Future, OpenCTI, Microsoft Sentinel, Defender, Splunk, Elastic Security and Google SecOps integrations when connected to check coverage and test rules; works without them (rules marked untested).",
        "content": "cti-sigma-writing",
        "tags": ["builtin", "cti", "detection", "sigma"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-yara-writing",
        "name": "YARA Rule Writing",
        "description": (
            "Writes YARA rules to identify and classify malicious files: rule "
            "structure, text/hex/regex strings, conditions, PE module, best "
            "practices, naming convention, syntax checking and validation against "
            "sample corpora (VirusTotal retrohunt and livehunt, ANY.RUN YARA "
            "search, MalwareBazaar). Load it when the user asks for a YARA rule "
            "(\"write YARA for this sample or family\") or when a hash investigation "
            "or malware analysis surfaces a sample worth a static-content rule."
        ),
        "compatibility": "Uses VirusTotal (hunting and retrohunt), ANY.RUN, abuse.ch MalwareBazaar, Intel 471, Ransomware.live, Recorded Future and OpenCTI integrations when connected, plus the code interpreter for syntax checks; works without them in writing-only mode.",
        "content": "cti-yara-writing",
        "tags": ["builtin", "cti", "detection", "yara"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-kql-writing",
        "name": "KQL Query Writing",
        "description": (
            "Writes KQL (Kusto) hunting queries and detections for Microsoft "
            "Sentinel, Microsoft Defender XDR advanced hunting and Azure Log "
            "Analytics: core tables, operators, string operators, common detection "
            "patterns mapped to ATT&CK, analytics rule header, best practices, and "
            "read-only test runs. Load it when the user asks for a KQL query, a "
            "Sentinel or Defender detection or hunt, or wants to translate a "
            "malware or hash investigation finding into KQL."
        ),
        "compatibility": "Uses Microsoft Sentinel (run_sentinel_kql_query) and Microsoft Defender (advanced hunting) integrations when connected to test queries, plus MITRE ATT&CK; works without them (queries marked not executed).",
        "content": "cti-kql-writing",
        "tags": ["builtin", "cti", "detection", "kql"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-china-cyber-espionage",
        "name": "China Cyber Espionage",
        "description": (
            "Knowledge cell on Chinese state-sponsored cyber operations: MSS and "
            "PLA actors (APT41, APT10, APT31, Volt Typhoon, Salt Typhoon, Mustang "
            "Panda, APT27, Hafnium/Silk Typhoon), aliases, active and historical "
            "campaigns, TTP evolution, infrastructure patterns, tooling, "
            "intelligence gaps, and how to refresh it from live sources. Load it "
            "when the user asks about PRC-aligned threat actors, MSS/PLA-attributed "
            "campaigns, Chinese pre-positioning in critical infrastructure, or PRC "
            "sector targeting."
        ),
        "compatibility": "Uses OpenCTI, MITRE ATT&CK, Feedly, Google Threat Intelligence, CrowdStrike, Recorded Future and Microsoft Defender TI integrations when connected to refresh the baseline; works without them as a dated reference.",
        "content": "cti-china-cyber-espionage",
        "tags": ["builtin", "cti", "knowledge", "nation-state", "china"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-russia-cyber-espionage",
        "name": "Russia Cyber Espionage",
        "description": (
            "Knowledge cell on Russian state-sponsored cyber operations: GRU, SVR "
            "and FSB actors (APT28, APT29, Sandworm, Turla, Gamaredon, Star "
            "Blizzard, Cadet Blizzard, Ember Bear), aliases, active and historical "
            "campaigns (SolarWinds, NotPetya, Ukraine grid attacks), wipers, TTP "
            "evolution, infrastructure, tooling, intelligence gaps, and how to "
            "refresh it from live sources. Load it when the user asks about Russian "
            "state actors, wartime ICS/OT or wiper campaigns, or pro-Russian cyber "
            "and information operations."
        ),
        "compatibility": "Uses OpenCTI, MITRE ATT&CK, Feedly, Google Threat Intelligence, CrowdStrike, Recorded Future and Microsoft Defender TI integrations when connected to refresh the baseline; works without them as a dated reference.",
        "content": "cti-russia-cyber-espionage",
        "tags": ["builtin", "cti", "knowledge", "nation-state", "russia"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-iran-cyber-espionage",
        "name": "Iran Cyber Espionage",
        "description": (
            "Knowledge cell on Iranian state-sponsored cyber operations: IRGC and "
            "MOIS actors (APT33, APT34/OilRig, APT35/Charming Kitten, MuddyWater, "
            "Moses Staff, Agrius, Cotton Sandstorm, Scarred Manticore, Imperial "
            "Kitten), aliases, campaigns, wipers, fake hacktivist personas, TTP "
            "evolution, infrastructure, tooling, intelligence gaps, and how to "
            "refresh it from live sources. Load it when the user asks about Iranian "
            "threat actors, Iranian wiper or hack-and-leak campaigns, or hacktivist "
            "personas in the Iran-Israel cyber conflict."
        ),
        "compatibility": "Uses OpenCTI, MITRE ATT&CK, Feedly, Google Threat Intelligence, CrowdStrike, Recorded Future and Microsoft Defender TI integrations when connected to refresh the baseline; works without them as a dated reference.",
        "content": "cti-iran-cyber-espionage",
        "tags": ["builtin", "cti", "knowledge", "nation-state", "iran"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-dprk-cyber-espionage",
        "name": "DPRK Cyber Operations",
        "description": (
            "Knowledge cell on North Korean state-sponsored cyber operations: "
            "Lazarus, APT38/BlueNoroff, Kimsuky, Andariel, TraderTraitor, APT43, "
            "ScarCruft; cryptocurrency and DeFi theft, the IT-worker fraudulent "
            "employment scheme, defense and nuclear espionage, supply-chain attacks "
            "(3CX), tooling and intelligence gaps. Load it when the user asks about "
            "DPRK or North Korean actors, revenue-generation campaigns, fake "
            "recruiter lures, crypto heists or DPRK targeting of developers and "
            "supply chains."
        ),
        "compatibility": "Uses CrowdStrike, Google Threat Intelligence, Recorded Future, Intel 471, Microsoft Defender TI, Feedly, OpenCTI, MISP, MITRE ATT&CK and abuse.ch integrations when connected to refresh the baseline; works without them from the built-in baseline.",
        "content": "cti-dprk-cyber-espionage",
        "tags": ["builtin", "cti", "knowledge", "nation-state", "dprk"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-ransomware-ecosystem",
        "name": "Ransomware Ecosystem",
        "description": (
            "Knowledge cell on the ransomware ecosystem: the RaaS model and "
            "affiliate markets, group lineage and rebrands (LockBit, "
            "ALPHV/BlackCat, RansomHub, Akira, Play, Black Basta, Cl0p, Medusa, "
            "BlackSuit, BianLian), law enforcement disruptions, leak-site and "
            "negotiation behaviour, TTPs, tooling and gaps. Load it when the user "
            "asks about ransomware groups, recent leak-site victims, attribution "
            "between groups, RaaS dynamics or sector targeting trends."
        ),
        "compatibility": "Uses the Ransomware.live integration for live leak-site data, plus CrowdStrike, Google Threat Intelligence, Recorded Future, Intel 471, VulnCheck, Feedly, OpenCTI and MITRE ATT&CK when connected; works without them from the built-in baseline.",
        "content": "cti-ransomware-ecosystem",
        "tags": ["builtin", "cti", "knowledge", "ransomware", "cybercrime"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-infostealers",
        "name": "Infostealers",
        "description": (
            "Knowledge cell on infostealer malware: MaaS families (Lumma, RedLine, "
            "Vidar, StealC, Raccoon, Rhadamanthys, AMOS and others), log "
            "marketplaces (Russian Market, Genesis successors, Telegram), "
            "distribution by malvertising, SEO poisoning and fake CAPTCHA, "
            "session-cookie theft and the infostealer-to-ransomware pipeline. Load "
            "it when the user asks about a stealer family, stealer-driven "
            "incidents, log markets or the organization's credential exposure in "
            "stealer logs."
        ),
        "compatibility": "Uses abuse.ch, Recorded Future, Flashpoint, Intel 471, Cybersixgill, SOCRadar, Google Threat Intelligence, VirusTotal, ANY.RUN, Feedly and OpenCTI integrations when connected; works without them from the built-in baseline.",
        "content": "cti-infostealers",
        "tags": ["builtin", "cti", "knowledge", "malware", "cybercrime", "credentials"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-initial-access-brokers",
        "name": "Initial Access Brokers",
        "description": (
            "Knowledge cell on initial access brokers (IABs): how corporate access "
            "is acquired (stealer logs, edge-device exploitation), packaged, priced "
            "and sold on Exploit, XSS, RAMP and Telegram, notable broker handles, "
            "the IAB-to-ransomware pipeline, pricing benchmarks and gaps, plus a "
            "method to assess whether a listing matches the organization. Load it "
            "when the user asks about access listings, broker handles, access "
            "pricing or whether the organization's access is for sale."
        ),
        "compatibility": "Uses Intel 471 (marketplaces, forum posts, instant messages), Flashpoint communities, Cybersixgill, SOCRadar dark web, Google Threat Intelligence DTM, Recorded Future, VulnCheck initial access, Shodan, GreyNoise, CrowdSec and OpenCTI integrations when connected; works without them from the built-in baseline.",
        "content": "cti-initial-access-brokers",
        "tags": ["builtin", "cti", "knowledge", "cybercrime", "dark-web"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-hacktivism",
        "name": "Hacktivism",
        "description": (
            "Knowledge cell on hacktivism: pro-Russian, pro-Ukrainian, pro-Iranian "
            "and pro-Palestinian groups (KillNet, NoName057(16), IT Army of "
            "Ukraine, Anonymous Sudan, CyberArmyofRussia_Reborn, Cyber Av3ngers and "
            "others), DDoS-claiming crews, state-aligned personas, ICS/OT claims, "
            "Telegram coordination, tooling and gaps. Load it when the user asks "
            "about hacktivist groups, DDoS claims against the organization or its "
            "sector, politically motivated operations or wartime cyber chatter."
        ),
        "compatibility": "Uses Intel 471, Flashpoint, Cybersixgill, SOCRadar, Google Threat Intelligence, Feedly, CrowdStrike, Recorded Future, Microsoft Defender TI, SIEM (Sentinel, Splunk, Elastic), Shodan, GreyNoise, CrowdSec and OpenCTI integrations when connected; works without them from the built-in baseline.",
        "content": "cti-hacktivism",
        "tags": ["builtin", "cti", "knowledge", "hacktivism", "ddos"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-carding-financial-fraud",
        "name": "Carding and Financial Fraud",
        "description": (
            "Knowledge cell on carding and financial fraud: card shops and "
            "autoshops (BidenCash, BriansClub, Russian Market, B1ack's Stash), "
            "Magecart digital skimming, CVV2, dumps and fullz trade, BIN attacks "
            "and card checkers, money mules, SIM swapping, BEC overlap and "
            "Fraud-as-a-Service. Load it when asked about payment-card fraud, "
            "card-shop markets, skimming, fraud TTPs or fraud actors (FIN7, "
            "Scattered Spider), or to give background to an assessment or briefing."
        ),
        "compatibility": "Uses OpenCTI, MISP, Flashpoint, Intel 471, Cybersixgill, SOCRadar, Recorded Future, GTI, urlscan, VirusTotal, abuse.ch, DomainTools and Feedly when connected to refresh the picture; the baseline works without them.",
        "content": "cti-carding-financial-fraud",
        "tags": ["builtin", "cti", "knowledge", "cybercrime", "fraud"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-phishing-social-engineering",
        "name": "Phishing and Social Engineering",
        "description": (
            "Knowledge cell on phishing and social engineering: AitM phishing kits "
            "and PhaaS platforms (EvilProxy, Tycoon 2FA, Greatness, Evilginx), MFA "
            "bypass, BEC, quishing, vishing and callback phishing, consent and "
            "device-code phishing, AI-enabled impersonation, and the actors behind "
            "them. Load it when asked about phishing campaigns or kits, BEC, "
            "pretexting, brand impersonation or lookalike domains, or to give "
            "background to a phishing assessment or awareness briefing."
        ),
        "compatibility": "Uses urlscan, VirusTotal, abuse.ch, ANY.RUN, DomainTools, Doppel, SOCRadar, GTI, Shodan, Microsoft Defender / Sentinel, Splunk, Sublime or Proofpoint, OpenCTI, MISP, MITRE ATT&CK and Feedly when connected; the baseline works without them.",
        "content": "cti-phishing-social-engineering",
        "tags": ["builtin", "cti", "knowledge", "phishing", "social-engineering"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-supply-chain-threats",
        "name": "Supply Chain Threats",
        "description": (
            "Knowledge cell on software and service supply chain attacks: vendor "
            "build and update compromise (SolarWinds, 3CX, CCleaner), MSP and RMM "
            "compromise (Kaseya), mass exploitation of file-transfer software "
            "(Cl0p: MOVEit, GoAnywhere, Cleo), open-source registry poisoning and "
            "maintainer takeover (XZ Utils), CI/CD targeting, and SLSA / SBOM "
            "defences. Load it when asked about supply chain or third-party "
            "compromise, malicious packages, dependency risk or vendor-compromise "
            "briefings."
        ),
        "compatibility": "Uses OpenCTI, MISP, Vulnerability Intelligence, VulnCheck, GreyNoise, CrowdSec, Shodan, Microsoft Defender, CrowdStrike, GTI, Ransomware.live, VirusTotal, abuse.ch, ANY.RUN, Recorded Future, MITRE ATT&CK and Feedly when connected; the baseline works without them.",
        "content": "cti-supply-chain-threats",
        "tags": ["builtin", "cti", "knowledge", "supply-chain"],
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
    {
        "slug": "cti-darkweb-collection",
        "name": "Dark Web Collection",
        "description": (
            "Defender-oriented methodology for dark-web collection: vendor-first "
            "access posture, selector design from PIRs, sourced reference lists of "
            "35+ underground forums and 30+ Telegram channels, OPSEC primer, "
            "passive-monitoring cadence and triage, Admiralty starting ratings, and "
            "local keyword matching over collected text. Load it when asked to "
            "design or run dark-web or Telegram monitoring, build a selector list, "
            "choose a dark-web intelligence vendor, check a forum or channel's "
            "status, or triage underground mentions of an organization."
        ),
        "compatibility": "Uses Flashpoint, Intel 471, Cybersixgill, SOCRadar, Recorded Future, GTI Digital Threat Monitoring and Ransomware.live for collection, OpenCTI / MISP and enrichment integrations (VirusTotal, Shodan, GreyNoise, AbuseIPDB, urlscan, DomainTools, abuse.ch) for pivots, and code_interpreter for keyword matching; works in methodology-only mode without them.",
        "content": "cti-darkweb-collection",
        "tags": ["builtin", "cti", "collection", "darkweb", "telegram", "opsec"],
        "resources": True,
        "license": _LICENSE_NOTICE,
        "resource_files": _LICENSE,
        "platform_targets": _PLATFORM_TARGETS,
    },
]
