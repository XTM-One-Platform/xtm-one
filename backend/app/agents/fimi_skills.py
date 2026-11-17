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

"""FIMI skills: the procedures of the FIMI Analyst, from the SGDSN/VIGINUM doctrine.

The FIMI Analyst's persona holds the rules that are always true and its two
knowledge bases (``viginum-fimi-doctrine``, ``viginum-fimi-publications``) hold
the facts; these skills hold the procedures, one per task an analyst brings:
capitalise a report, assess an information manipulation set, review an
attribution, map behaviours to DISARM, pivot on infrastructure, read
coordination indicators, write a brief, review a source's corpus. Each step cites the doctrine section or
the VIGINUM report it comes from, so the agent can check it in its knowledge
bases. Progressive disclosure keeps their bodies out of the per-turn prompt:
only the catalog line is inline, ``load_skill`` reads the body.

Bodies live in ``seed_data/skill_content/<slug>.md``. The sources are described
in ``seed_data/NOTICE-viginum-fimi.md``.
"""

from __future__ import annotations

#: The modes the FIMI skills ship in, those of the FIMI built-in agents.
_PLATFORM_TARGETS = ["xtm_one", "dev"]

_ANALYST = "fimi-analyst"
_INVESTIGATOR = "fimi-infrastructure-investigator"

FIMI_SKILLS: list[dict] = [
    {
        "slug": "fimi-report-capitalisation",
        "name": "FIMI Report Capitalisation",
        "description": (
            "Turn a source document (a VIGINUM or vendor report, a press article, "
            "an official statement) into a capitalised OpenCTI Report following "
            "the SGDSN/VIGINUM doctrine: read and segment the source, find what "
            "already exists, fill the eight Overview fields, name the annual "
            "campaign, build the star graph from the campaign with the canonical "
            "relationships and their direction, choose each entity by its role, "
            "date every relationship, present the plan and write it in a draft "
            "once the analyst confirms. Load it whenever a document or a case is to "
            "be capitalised, imported, modelled or recorded in OpenCTI ('capitalise "
            "this report', 'how do I model this case?', 'propose the OpenCTI plan'), "
            "and before completing a report."
        ),
        "compatibility": "OpenCTI integration (reads; writes in a draft after confirmation); the VIGINUM FIMI knowledge bases.",
        "content": "fimi-report-capitalisation",
        "tags": ["builtin", "fimi", "opencti", "capitalisation"],
        "platform_targets": _PLATFORM_TARGETS,
        "bind_to_agent_slugs": [_ANALYST],
    },
    {
        "slug": "fimi-ims-assessment",
        "name": "FIMI IMS Assessment",
        "description": (
            "Decide whether observed activity is an information manipulation set "
            "(IMS, French MOI) and how to model it: the five levels of an "
            "operation, the two cumulative conditions (clandestinity and "
            "coordination) with VIGINUM's own positive and negative examples, "
            "actor versus IMS, comparison with the sets VIGINUM documented and "
            "their aliases, and the decision to create, reuse, alias, keep apart "
            "or merge an Intrusion Set. Load it whenever a set's nature or identity "
            "is in question ('is this a known operation?', 'is it an IMS?', 'is it "
            "the same set as Matriochka?', 'should I merge these sets?')."
        ),
        "compatibility": "OpenCTI integration (reads); the VIGINUM FIMI knowledge bases.",
        "content": "fimi-ims-assessment",
        "tags": ["builtin", "fimi", "opencti", "ims"],
        "platform_targets": _PLATFORM_TARGETS,
        "bind_to_agent_slugs": [_ANALYST],
    },
    {
        "slug": "fimi-attribution-review",
        "name": "FIMI Attribution Review",
        "description": (
            "Record the attribution a document states, or audit the attributions "
            "already in OpenCTI, under the doctrine: imputation versus "
            "attribution, the three cases (technical evidence, public "
            "attribution statement, unsubstantiated mention), the origin actor "
            "always shown, attribution dated at the stating document's "
            "publication with no end date, and how VIGINUM words certainty and "
            "doubt in its reports. Proposes corrections, never applies them "
            "without confirmation. Load it whenever attribution or origin is at "
            "stake ('should I attribute this to Russia?', 'who is behind it?', "
            "'is this attribution supported?', 'it is called pro-Kremlin, so...') "
            "and before writing any attributed-to relationship."
        ),
        "compatibility": "OpenCTI integration (reads; writes in a draft after confirmation); the VIGINUM FIMI knowledge bases.",
        "content": "fimi-attribution-review",
        "tags": ["builtin", "fimi", "opencti", "attribution"],
        "platform_targets": _PLATFORM_TARGETS,
        "bind_to_agent_slugs": [_ANALYST],
    },
    {
        "slug": "fimi-disarm-mapping",
        "name": "FIMI DISARM Mapping",
        "description": (
            "Map the observed behaviours of an information operation to DISARM "
            "techniques and record them as OpenCTI Attack Patterns: describe "
            "each behaviour, walk the tactics, look the candidates up in the "
            "French-English DISARM referential (never an identifier from "
            "memory), pick the most specific sub-technique the evidence "
            "supports, reuse the existing Attack Pattern, link it from the "
            "campaign with dates. Covers the gaps between the referential and "
            "the identifiers VIGINUM's reports cite. Load it whenever techniques "
            "or TTPs are asked for ('which DISARM techniques?', 'what are its "
            "TTPs?') and before naming any DISARM technique."
        ),
        "compatibility": "OpenCTI integration (reads; writes in a draft after confirmation); the VIGINUM FIMI Doctrine knowledge base.",
        "content": "fimi-disarm-mapping",
        "tags": ["builtin", "fimi", "opencti", "disarm"],
        "platform_targets": _PLATFORM_TARGETS,
        "bind_to_agent_slugs": [_ANALYST],
    },
    {
        "slug": "fimi-infrastructure-pivoting",
        "name": "FIMI Infrastructure Pivoting",
        "description": (
            "Link the digital assets of an operation with the technical pivots "
            "VIGINUM uses (registration and creation waves, hosting and ASN, "
            "page source and fingerprints, redirection chains and geofencing, "
            "typosquatting, backlink networks, naming patterns), rate the "
            "strength of each link, and record the cluster: attacker sites as "
            "Channels, legitimate or impersonated sites as Domain Name "
            "observables, observables related to the campaign. Load it whenever "
            "assets may be linked ('are these domains linked?', 'what else "
            "belongs to this network?', 'who hosts these sites?') and before "
            "pivoting on domains, URLs or IPs."
        ),
        "compatibility": "OpenCTI integration, web_fetch, and the connected enrichment integrations if any; the VIGINUM FIMI Publications knowledge base.",
        "content": "fimi-infrastructure-pivoting",
        "tags": ["builtin", "fimi", "opencti", "infrastructure"],
        "platform_targets": _PLATFORM_TARGETS,
        "bind_to_agent_slugs": [_ANALYST, _INVESTIGATOR],
    },
    {
        "slug": "fimi-coordination-indicators",
        "name": "FIMI Coordination Indicators",
        "description": (
            "Recognise coordinated inauthentic behaviour and impersonation from "
            "the indicators VIGINUM documents (seeders and quoters, copy-pasta, "
            "publication bursts, engagement anomalies, renamed accounts, "
            "generated avatars and text leftovers, shared followers, paid "
            "influencers, laundering through paid media, impersonated media and "
            "institutions), weigh them alone and combined, and record accounts "
            "as Channels, amplification and published media content. Load it "
            "whenever accounts or posts may be inauthentic ('are these accounts "
            "coordinated?', 'are these bots?', 'is this campaign astroturfed?') "
            "and before judging coordination."
        ),
        "compatibility": "OpenCTI integration, the FIMI analytics tools; the VIGINUM FIMI Publications knowledge base.",
        "content": "fimi-coordination-indicators",
        "tags": ["builtin", "fimi", "opencti", "coordination"],
        "platform_targets": _PLATFORM_TARGETS,
        "bind_to_agent_slugs": [_ANALYST, _INVESTIGATOR],
    },
    {
        "slug": "fimi-threat-brief",
        "name": "FIMI Threat Brief",
        "description": (
            "Write a FIMI threat brief, technical report or situation report the "
            "way VIGINUM structures its own: synthesis first, victimology and "
            "contents, diffusion scheme, infrastructure, attribution elements in "
            "VIGINUM's hedged wording, DISARM and indicator annexes; every claim "
            "tied to OpenCTI data or a cited source, propaganda described as "
            "content, indicators defanged, marking and dates exact, numbers "
            "computed rather than estimated. Load it whenever a written product is "
            "asked for ('write a note on...', 'summarise what is known about...', "
            "'a brief, a report, a synthesis') and before writing it."
        ),
        "compatibility": "OpenCTI integration, the FIMI analytics tools; the VIGINUM FIMI knowledge bases.",
        "content": "fimi-threat-brief",
        "tags": ["builtin", "fimi", "opencti", "report"],
        "platform_targets": _PLATFORM_TARGETS,
        "bind_to_agent_slugs": [_ANALYST],
    },
    {
        "slug": "fimi-corpus-review",
        "name": "FIMI Corpus Review",
        "description": (
            "Answer a question that spans many documents of one source (fact-check "
            "cases, articles, posts, search hits over a period) without reading them "
            "all in the conversation: learn the result's shape on one small page, "
            "ingest the pages into the workset with to_workset (each item kept "
            "verbatim with its own id, date and link), check every receipt, seal, "
            "then list, search and read only what the question needs, and state the "
            "coverage. Load it whenever an answer needs more than one page of a "
            "source ('which narratives over the quarter?', 'every case on...', 'how "
            "many...', 'what does this source document about... since 2022?')."
        ),
        "compatibility": "The workset tools; any read tool of a source (MCP server, integration, web_fetch).",
        "content": "fimi-corpus-review",
        "tags": ["builtin", "fimi", "workset", "corpus"],
        "platform_targets": _PLATFORM_TARGETS,
        "bind_to_agent_slugs": [_ANALYST],
    },
]
