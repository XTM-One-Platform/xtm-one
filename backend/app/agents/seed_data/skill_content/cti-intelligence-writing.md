# Intelligence Writing

Intelligence writing is not creative writing. It is precise, structured, and audience-driven. Every word must earn its place.

Use this skill to draft any finished intelligence product: flash report, intelligence summary, threat assessment, threat actor profile or campaign report. For the distinct structure of threat, risk and vulnerability assessments, load the **Writing Assessments** skill if it is attached. For the quality gate before dissemination, load the **Intelligence Quality Control** skill if it is attached.

## Core Principles

### 1. Bottom Line Up Front (BLUF)
The most important finding goes in the first paragraph. If the reader stops after two sentences, they should have the key takeaway.

**Bad**: "Over the past several months, we have observed various indicators that suggest a potential shift in threat actor targeting patterns across multiple sectors..."

**Good**: "APT41 is actively targeting European pharmaceutical companies using a new loader variant. We assess with high confidence that at least three organisations in this sector have been compromised since January 2026."

### 2. Active Voice
Use active voice. Name the actor.

- Wrong: "The malware was deployed by the threat actor"
- Right: "The threat actor deployed the malware"
- Wrong: "It was assessed that..."
- Right: "We assess that..."

### 3. Distinguish Facts, Assessments, and Assumptions

| Type | Signal words | Example |
|------|-------------|---------|
| **Fact** | "observed", "confirmed", "identified" | "We identified three C2 domains registered in January 2026." |
| **Assessment** | "we assess", "indicates", "suggests" | "We assess with high confidence that these domains are linked to APT41." |
| **Assumption** | "assuming", "if", "given that" | "Assuming the registration pattern continues, we expect additional infrastructure in Q2." |

### 4. Use Mandated Language

Apply these rules inline. The full guidance lives in the **Confidence Levels**, **Likelihood Language**, **Source Assessment (Admiralty Scale)** and **TLP Guide** skills; load at most one or two of them when you need the detail.

- **Confidence** (quality of the evidence, not probability): Very High (90-100), High (75-89), Moderate (50-74), Low (25-49), Very Low (0-24). Every analytic judgment carries a band and, where useful, a score and a one-line rationale.
- **Likelihood** (forward-looking statements only), with the range in parentheses: Almost certain (>95%), Highly likely (75-95%), Likely (50-75%), Realistic possibility (25-50%), Unlikely (15-25%), Highly unlikely (5-15%), Remote (<5%). Never "could", "may" or "might" without a band; never combine two bands.
- **Source assessment** (Admiralty Scale): reliability A (completely reliable) to F (cannot be judged), credibility 1 (confirmed) to 6 (cannot be judged), written as e.g. B2. Every source in the Sources table carries a rating.
- **TLP 2.0**: CLEAR, GREEN, AMBER, AMBER+STRICT, RED. Every product carries a marking at the top; it can never be lower than the most restrictive source it uses.

### 5. Be Specific
- Wrong: "Recently, a sophisticated threat actor..."
- Right: "In March 2026, APT41 (also tracked as Winnti, Barium)..."
- Wrong: "Multiple indicators were found"
- Right: "We identified 14 IP addresses and 3 domain names associated with this campaign"

## Producing the Finished Product in XTM One

The templates below describe the content and section order. Choose the delivery format from the request:

- **In the chat or run output** (default): write the product as Markdown following the template.
- **A structured, printable report** (the user asks for a report, brief, executive summary or management readout): use the `generate_report` tool. Map each template section to a report block, put the date, TLP marking, confidence and audience in the header tags, and put the BLUF / key finding in the header verdict. Include the returned report marker verbatim in your reply. To revise it, call `generate_report` again with the same report id and the full block list.
- **A downloadable file** (Markdown, DOCX, CSV of IOCs, JSON): use `generate_file`.
- **In OpenCTI** (only when the user or the assignment asks for it): with the OpenCTI integration, create the Report container with `create_opencti_container`, attach the entities and observables it covers with `add_opencti_to_container`, and set the TLP with `add_opencti_marking`. For a short analytic comment on an existing entity, use `create_opencti_note`.

Use only the tools you actually have; when an integration is not connected, say which output channel was skipped instead of pretending the product was published.

The metadata block at the top of each template (title, type, date, TLP, confidence, author, status, related PIRs, ATT&CK techniques, tags) becomes the report header, the OpenCTI Report fields, or a header table in a Markdown product. Keep it: it is what makes the product traceable to a PIR and searchable later.

## Product Templates

### Flash Report
Time-critical intelligence requiring immediate attention. 1-2 pages maximum.

```markdown
<!-- metadata -->
title: "Flash Report: [Subject]"
type: flash-report
date: YYYY-MM-DD
tlp: AMBER
confidence: [level]
author: analyst
status: draft
related_pirs: []
mitre_attack: []
tags: []

# Flash Report: [Subject]
**TLP:[LEVEL]** | **Date:** YYYY-MM-DD | **Confidence:** [Level]

## Key Finding
[1-2 sentences. BLUF. What happened, who is affected, what should be done.]

## Details
[3-5 paragraphs maximum. What we know, how we know it, what it means.]

## Indicators of Compromise
| Type | Value | Context |
|------|-------|---------|
| IP | x.x.x.x | C2 server |
| Domain | example.com | Phishing landing page |
| SHA256 | abc123... | Loader variant |

## Recommended Actions
1. [Immediate action]
2. [Detection action]
3. [Investigation action]

## Sources
| Source | Reliability | Key Information |
|--------|------------|-----------------|
| [Source] | [Admiralty] | [What it contributed] |
```

### Intelligence Summary
Periodic overview of a topic or time period. 2-4 pages.

```markdown
<!-- metadata -->
title: "Intelligence Summary: [Subject/Period]"
type: intelligence-summary
date: YYYY-MM-DD
tlp: GREEN
confidence: [level]
author: analyst
status: draft
related_pirs: []
tags: []

# Intelligence Summary: [Subject/Period]
**TLP:[LEVEL]** | **Date:** YYYY-MM-DD | **Period:** [Coverage period]

## Executive Summary
[2-3 paragraphs. Key developments, trends, and implications.]

## Key Developments
### [Development 1]
[Assessment with confidence level and likelihood language where applicable.]

### [Development 2]
[...]

## Trend Analysis
[How does this period compare to previous? What's changing?]

## Outlook
[Forward-looking assessment using likelihood language.]

## Sources
[Sourcing table with Admiralty ratings]
```

### Threat Assessment
Structured assessment of a specific threat. 3-6 pages.

```markdown
<!-- metadata -->
title: "Threat Assessment: [Subject]"
type: threat-assessment
date: YYYY-MM-DD
tlp: AMBER
confidence: [level]
author: analyst
status: draft
related_pirs: []
mitre_attack: []
tags: []

# Threat Assessment: [Subject]
**TLP:[LEVEL]** | **Date:** YYYY-MM-DD | **Confidence:** [Level]

## Executive Summary
[BLUF: Overall threat level and key finding.]

## Threat Level: [CRITICAL/HIGH/MODERATE/LOW/NEGLIGIBLE]

## Intent
[What does the threat actor want to achieve? Evidence for assessed intent.]

## Capability
[What can the threat actor do? Technical sophistication, resources, tooling.]

## Opportunity
[What attack surface exists? Vulnerability landscape, exposure.]

## Assessment
[Combined analysis: Intent x Capability x Opportunity. Confidence and likelihood language.]

## Key Assumptions
[List and evaluate key assumptions underlying this assessment.]

## MITRE ATT&CK Mapping
| Tactic | Technique | Notes |
|--------|-----------|-------|
| Initial Access | T1566 Phishing | Primary delivery method |

## Recommended Mitigations
1. [Prioritised by impact]

## Sources
[Sourcing table with Admiralty ratings]
```

### Threat Actor Profile
Comprehensive profile of a threat group. 4-8 pages. The detailed profile template and the collection workflow live in the **Threat Actor Profiling** skill.

```markdown
<!-- metadata -->
title: "Threat Actor Profile: [Name/Designation]"
type: threat-actor-profile
date: YYYY-MM-DD
tlp: GREEN
confidence: [level]
author: analyst
status: draft
tags: []

# Threat Actor Profile: [Name/Designation]
**TLP:[LEVEL]** | **Last Updated:** YYYY-MM-DD

## Summary
| Field | Value |
|-------|-------|
| **Aliases** | [List all known aliases] |
| **Attribution** | [State/criminal group + confidence] |
| **Motivation** | [Espionage/Financial/Disruption/Hacktivism] |
| **Active Since** | [Year] |
| **Primary Targets** | [Sectors, geographies] |
| **Sophistication** | [Low/Medium/High/Advanced] |

## Overview
[2-3 paragraphs summarising the group, its activities, and significance.]

## Targeting
[Who they target, how targeting has evolved, victimology patterns.]

## TTPs (MITRE ATT&CK)
| Tactic | Technique | Description |
|--------|-----------|-------------|

## Tooling
| Tool | Type | First Seen | Notes |
|------|------|-----------|-------|

## Infrastructure
[Known infrastructure patterns, hosting preferences, C2 frameworks.]

## Campaign History
### [Campaign Name] (Date range)
[Summary of campaign, victims, TTPs, outcomes.]

## Intelligence Gaps
[What we don't know and need to collect.]

## Sources
[Sourcing table with Admiralty ratings]
```

### Campaign Report
Documentation of a specific campaign. 3-6 pages. The full campaign record and lifecycle live in the **Campaign Tracking** skill.

```markdown
<!-- metadata -->
title: "Campaign Report: [Name/Identifier]"
type: campaign-report
date: YYYY-MM-DD
tlp: AMBER
confidence: [level]
author: analyst
status: draft
related_pirs: []
mitre_attack: []
tags: []

# Campaign Report: [Name/Identifier]
**TLP:[LEVEL]** | **Date:** YYYY-MM-DD | **Confidence:** [Level]

## Executive Summary
[BLUF: Who, what, when, where, impact.]

## Timeline
| Date | Event |
|------|-------|
| YYYY-MM-DD | [Event description] |

## Attribution
[Assessment with confidence level. Link to threat actor profile.]

## Victimology
[Targeted organisations, sectors, geographies. Selection criteria if known.]

## Attack Chain
[Step-by-step technical description mapped to MITRE ATT&CK.]

## Diamond Model
| Vertex | Details |
|--------|---------|
| **Adversary** | [Actor/group] |
| **Capability** | [Tools, exploits, techniques] |
| **Infrastructure** | [C2, staging, delivery infrastructure] |
| **Victim** | [Targeted entities] |

## IOCs
[Full IOC table]

## Detection Guidance
[SIGMA/YARA/KQL rules or detection logic]

## Sources
[Sourcing table with Admiralty ratings]
```

## Verifying What You Write

- Resolve every ATT&CK technique ID you cite. With the MITRE ATT&CK integration: `search_mitre_attack` or `get_mitre_attack_object`. Do not cite a technique ID from memory without checking it when the integration is available.
- When the product draws on entities already in OpenCTI (intrusion sets, malware, campaigns, reports), check them with `search_opencti` / `get_opencti_entity` so names, aliases and dates match the knowledge base.
- Defang every indicator in prose and IOC tables intended for humans (`hxxp`, `[.]`), unless the product is a machine-readable export. `code_interpreter` can defang a long list reliably.

## Writing Checklist

Before submitting any product:
- [ ] BLUF in first paragraph?
- [ ] TLP marking present and appropriate?
- [ ] All assessments carry confidence levels?
- [ ] All forward-looking statements use likelihood language?
- [ ] All sources assessed with Admiralty Scale?
- [ ] Facts distinguished from assessments?
- [ ] Active voice throughout?
- [ ] Specific dates, numbers, and names (not "recently" or "several")?
- [ ] Key assumptions identified?
- [ ] MITRE ATT&CK techniques mapped where applicable?
