# PIR Management

PIRs (Priority Intelligence Requirements) are the foundation of relevant intelligence. They define what stakeholders need to know and drive all collection and analysis priorities. Without PIRs, a CTI team produces intelligence nobody asked for.

## What is a PIR?

A PIR is a question that, when answered, directly supports a stakeholder's decision-making. PIRs are:
- **Stakeholder-driven** - not what the analyst finds interesting
- **Decision-oriented** - answering them enables action
- **Bounded** - specific enough to collect against
- **Reviewable** - can be assessed for satisfaction

## PIR Format (SMART)

Each PIR should be:
- **S**pecific: Clear scope and focus
- **M**easurable: Can determine when it's answered
- **A**chievable: Can be collected against with available resources
- **R**elevant: Directly supports stakeholder decisions
- **T**ime-bound: Has a review/expiry date

### PIR Record Template

This is the full analytic record of a PIR, whatever the storage:

```yaml
pir_id: PIR-001
title: "What ransomware groups are actively targeting the European healthcare sector?"
created: 2026-01-15
review_date: 2026-04-15
status: active
priority: high
stakeholder: CISO
collection_sources: [osint, opencti, ransomware_live, feedly]
knowledge_cells: [ransomware-ecosystem]
last_satisfied: 2026-03-01
satisfaction_count: 3
```

```markdown
## Context
Why this PIR matters to the stakeholder. What decisions depend on the answer.

## Standing Requirements
- Monitor for new ransomware campaigns targeting healthcare
- Track ransomware group infrastructure changes
- Assess likelihood of targeting our specific organisation

## Collection Guidance
- OSINT: Monitor vendor reports, ISAC feeds, government advisories
- Technical: Enrich IOCs from healthcare-targeted campaigns
- Dark web: Monitor ransomware leak sites for healthcare victims

## Satisfaction History
| Date | Product | Summary |
|------|---------|---------|
| 2026-03-01 | Flash Report FR-2026-03-01 | New ransomware campaign targeting EU hospitals |
| 2026-02-15 | Intelligence Summary IS-2026-02-15 | Monthly ransomware landscape update |
```

## Where PIRs Live in XTM One

Use only the tools you actually have; when an integration is not connected, say so and fall back as described below. Create, modify or delete a PIR only when the user or the assignment asks for it; listing and reading are always fine.

### With the OpenCTI integration (preferred)

OpenCTI has native PIRs. An OpenCTI PIR does not store the question as free text only: it **watches relationship events** and flags the source entities (intrusion sets, malware, campaigns...) whose relationships match its weighted criteria, giving each a PIR score. That makes the PIR self-collecting.

| Tool | Use |
|------|-----|
| `manage_opencti_pir` action `list` / `get` | Read the PIR set, or one PIR with its scope filters and weighted criteria. |
| `manage_opencti_pir` action `create` | Create a PIR: `name`, `pir_type`, `description`, `pir_rescan_days`, `pir_filters`, `pir_criteria`. |
| `manage_opencti_pir` action `update` | Change name, description or rescan period. |
| `manage_opencti_pir` action `delete` | Remove a PIR (retirement, see below). |
| `query_opencti_pir_entities` | What is flagged in the PIR and why, ranked by PIR score (the main answer to "what satisfies this PIR?"). Filter by `entity_types` (e.g. Intrusion-Set, Malware). |
| `query_opencti_pir_relationships` | The underlying flagged relationships, by date window (`start_date` / `end_date`), source types or confidence: "what changed this week?". |
| `search_opencti` | Resolve the sectors, regions, countries or organisations the criteria point to: criteria need OpenCTI internal IDs, names are rejected. |

**Translating a SMART PIR into an OpenCTI PIR**

| PIR record field | OpenCTI PIR |
|------------------|-------------|
| `title` (the question) | `name` (short form) and the first line of `description` (full question) |
| Scope ("European healthcare sector") | `pir_criteria`: one criterion per target, each `{"weight": N, "filters": ...}` with an `entity_type` filter on the relationship type (usually `targets`) AND a `toId` filter on the target entity ID (Sector "Healthcare", Region "Europe", your Organization...). Higher weight for the targets that matter most. |
| Minimum quality bar | `pir_filters`: scope filter group, e.g. confidence greater than or equal to 60 (values as strings). |
| Type of question | `pir_type`: `THREAT_LANDSCAPE` (who targets my sectors / regions / organisation), `THREAT_ORIGIN` (activity coming from given origins), `THREAT_CUSTOM` (anything else). |
| Look-back window | `pir_rescan_days`: the rescan period in days (default 30 at creation). It is not the review date. |
| `priority`, `stakeholder`, `review_date`, `status`, context, standing requirements, collection guidance | Not native PIR fields: write them as a compact header block in `description` (for example `Priority: High | Stakeholder: CISO | Review: 2026-04-15`), followed by the context. |
| Satisfaction history | Products that answer the PIR are OpenCTI Reports whose entities are flagged in it. Keep the dated satisfaction log in agent memory (`store_memory`, keyed by the PIR name) or as an OpenCTI Note when the team prefers it there. |

PIR criteria must stay generic: they describe targets (sectors, regions, organisations), never a single indicator. Questions that are not about targeting (for example "which detection rules should we prioritise?") cannot be expressed as criteria; create them as `THREAT_CUSTOM` with an explanatory description, or keep them in memory.

### Without OpenCTI PIRs (fallback)

Keep the PIR set in the agent's memory: one memory entry per PIR containing the full PIR record (YAML header + sections) with `store_memory`, and retrieve it with `search_memory` (search by PIR ID, title or stakeholder). Say in the output that the PIR set lives in agent memory, so it is visible only to this agent. Retired PIRs stay in memory with `status: retired`.

## PIR Lifecycle

### 1. Creation
- Stakeholder engagement: "What do you need to know to do your job?"
- Draft PIR in SMART format
- Assign priority (Critical / High / Medium / Low)
- Map to collection sources and knowledge cells
- Set review date (default: quarterly)
- Store it: `manage_opencti_pir` action `create` (after resolving target IDs with `search_opencti`), or `store_memory`

### 2. Collection Planning
For each active PIR:
- Identify collection sources that can answer it (see the table below)
- Set up standing collection tasks (in XTM One, a scheduled assignment on a CTI agent that reads the PIR, queries the sources and reports what is new)
- Map to relevant knowledge cells for context
- Document collection guidance

| PIR family | Sources in XTM One (when connected) |
|------------|-------------------------------------|
| Who targets our sector / region | OpenCTI PIR flags, `targets` relationships; Feedly (`query_feedly_cyberattacks`, `search_feedly_articles`); GTI (`search_gti_threats`); Recorded Future (`search_rf_threat_actors`); CrowdStrike (`list_crowdstrike_actors`) |
| Ransomware against our sector | Ransomware.live (`search_ransomlive_victims`, `list_ransomlive_recent_victims`, `list_ransomlive_sectors`) |
| Exploited vulnerabilities in our stack | Vulnerability Intelligence (`search_vulnintel_kev`, `assess_vulnintel_cve`), VulnCheck (`get_vulncheck_kev`), Feedly (`query_feedly_vulnerability_dashboard`) |
| Our assets or credentials on the dark web | Flashpoint (`search_flashpoint_credentials`), Intel 471 (`search_intel471_credentials`, `search_intel471_data_leaks`), SOCRadar (`search_socradar_dark_web`), Recorded Future (`search_rf_leaked_credentials`), GTI DTM monitors (`list_gti_dtm_alerts`) |
| Current IOCs / TTPs against our sector | OpenCTI indicators and attack patterns linked to the flagged actors; MITRE ATT&CK (`get_mitre_attack_related`); abuse.ch ThreatFox (`search_abusech_iocs`) |
| Supply chain campaigns | OpenCTI campaigns targeting your suppliers (add the suppliers as Organization entities and as PIR criteria) |

### 3. Satisfaction
When intelligence answering a PIR is produced:
- Link the product to the PIR (cite the PIR in the product; in OpenCTI, a report whose relationships match the criteria is flagged automatically)
- Update `last_satisfied` date
- Increment `satisfaction_count`
- Add to satisfaction history (memory entry or Note, see above)

### 4. Review (Quarterly)
For each active PIR:
- Is this still relevant to the stakeholder?
- Has it been satisfactorily answered? (`query_opencti_pir_entities` shows what it has flagged and how strongly)
- Should priority change?
- Should scope be adjusted? (add/remove criteria targets or change weights)
- Should it be retired?

### 5. Retirement
When a PIR is no longer needed:
- Set `status: retired` and document the reason for retirement
- In OpenCTI: record the retirement reason first (Note, or memory), then delete the PIR with `manage_opencti_pir` action `delete` only when the user confirms; OpenCTI keeps no archive of deleted PIRs
- In memory: update the entry with `status: retired` and the reason

## PIR Priority Guide

| Priority | Criteria | Collection Effort |
|----------|---------|-------------------|
| **Critical** | Imminent threat to organisation, board-level concern, regulatory requirement | Dedicated resources, daily monitoring |
| **High** | Active threat landscape relevant to organisation, CISO priority | Regular monitoring, weekly reporting |
| **Medium** | General awareness need, emerging threat area | Periodic collection, monthly reporting |
| **Low** | Background intelligence, professional development | Opportunistic collection |

## Common PIR Examples

### Strategic PIRs
- "What nation-state actors are likely to target our sector in the next 12 months?"
- "How is the ransomware threat landscape evolving, and what are the implications for our risk posture?"
- "What emerging technologies or attack vectors could change our threat profile?"

### Operational PIRs
- "What TTPs are currently being used against organisations in our sector?"
- "Which vulnerabilities are being actively exploited that affect our technology stack?"
- "Are there any active campaigns targeting our supply chain?"

### Tactical PIRs
- "What IOCs are associated with current campaigns targeting our sector?"
- "What detection rules should we prioritise for the current threat landscape?"
- "Are any of our assets or credentials appearing on dark web marketplaces?"

## Orchestrator Integration

An orchestrating CTI agent should:
1. Check active PIRs before starting any new work
2. Route collection and analysis tasks to relevant PIRs
3. Tag all products with related PIR IDs
4. Alert when a Critical PIR hasn't been satisfied in 30+ days
5. Facilitate quarterly PIR reviews (the **CTI Standard Operating Procedures** skill, SOP-006)
