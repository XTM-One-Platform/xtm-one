# Threat Actor Profiling

A threat actor profile is a living document that consolidates everything known about a threat group. It grows over time as new intelligence is collected. Use this skill to build or update a profile ("tell me about actor X", "profile actor Y"), or when another product needs the canonical profile structure (attribution, TTPs, campaigns, infrastructure patterns, intelligence gaps).

## Collection Workflow

Use only the tools you actually have; when an integration is not connected, say which source was skipped instead of guessing its result. Rate every source you use (Admiralty Scale, below) and record it in the Sources table.

### Step 1 - Start from what the organisation already knows
Check this before rebuilding a profile from scratch.

- **OpenCTI** (internal knowledge base): `search_opencti` on the actor name and each alias to find the Intrusion Set (or Threat Actor Group / Individual), then `get_opencti_entity` for its description, aliases, first/last seen, motivations and sophistication. Walk the relationships with `list_opencti_relationships` (`uses` -> Attack Patterns, Malware, Tools; `targets` -> Sectors, Countries, Regions, Organizations; `attributed-to` <- Campaigns; `originates-from`), `get_opencti_top_related` for the most connected entities, and `get_opencti_entity_history` to see what changed recently. Reports containing the actor give the dated campaign history (`get_opencti_container_full` on the most relevant ones).
- **MISP**: `search_misp_events` with the actor's name or galaxy tag for prior internal cataloguing; `list_misp_galaxy_clusters` for the threat-actor galaxy entry and its synonyms.

### Step 2 - Vendor finished intelligence (primary feed for state-sponsored and espionage actors)

| Integration | Tools | What it gives |
|-------------|-------|---------------|
| CrowdStrike Falcon Intelligence | `list_crowdstrike_actors` (find the adversary by name), `get_crowdstrike_actor` | Adversary profile: origins, target countries and industries, motivations, capability, aliases, ATT&CK techniques |
| | `list_crowdstrike_intel_reports` (filter on the actor), `download_crowdstrike_intel_report` | Newest finished reporting |
| | `list_crowdstrike_intel_indicators` | Indicators attributed to the adversary |
| Google Threat Intelligence | `search_gti_threats`, `get_gti_threat`, `get_gti_threat_mitre_tree`, `get_gti_threat_timeline`, `get_gti_threat_related_objects`, `search_gti_threat_iocs` | Actor collection: aliases, targeting, ATT&CK tree, timeline of activity, related malware / campaigns / reports, IOCs |
| | `list_gti_threat_profiles`, `get_gti_threat_profile_recommendations` | When the organisation has a GTI threat profile: whether this actor is recommended as relevant to it |
| Recorded Future | `search_rf_threat_actors`, `lookup_rf_entity`, `get_rf_threat_map` | Actor entity, aliases, related entities, position on the organisation's threat map |
| | `search_rf_analyst_notes` | Recorded Future Insikt reporting |
| Intel 471 | `list_intel471_actors`, `get_intel471_actor` | Underground actor handles and activity (best for cybercriminal actors) |
| | `search_intel471_reports`, `search_intel471_forum_posts` | Finished reports and the actor's forum footprint |
| Feedly | `lookup_feedly_entity` / `search_feedly_entities`, `get_feedly_entities`, `get_feedly_entity_relationships` | Actor entity with aliases and related malware / CVEs / targets extracted from open reporting |
| | `search_feedly_articles` | Recent open-source reporting on the actor |
| Flashpoint | `search_flashpoint_reports` | Finished reporting, especially cybercrime |

### Step 3 - Reference data

- **MITRE ATT&CK**: `search_mitre_attack` to find the group (Gxxxx) and confirm aliases, `get_mitre_attack_object` for its description, `get_mitre_attack_related` for the techniques and software attributed to it. Resolve every technique ID from vendor sources against ATT&CK before it enters the TTPs table.
- **Ransomware actors (primary feed)**: Ransomware.live `get_ransomlive_group` returns the group's curated profile (TTPs, leak-site infrastructure, vulnerabilities exploited); `get_ransomlive_iocs` and `get_ransomlive_yara` give per-group IOC and YARA dumps; `search_ransomlive_victims` and `get_ransomlive_stats` give victimology and tempo.
- **Community IOCs**: abuse.ch ThreatFox (`search_abusech_iocs` with the malware family the actor uses) replaces the community pulse search the source used (AlienVault OTX has no XTM One integration).

### Step 4 - Fill gaps with open sources
Vendor and government reporting through `search_google_web` + `web_fetch` (see the **OSINT Methodology** skill). Prefer primary publications over re-reporting.

### Step 5 - Infrastructure cluster
Hand the indicators gathered above to the **Indicator Pivoting** skill to build the infrastructure picture (hosting, registrars, TLS and C2 patterns).

### Step 6 - Write and, when asked, record
Produce the profile below in the output (or as a structured report with `generate_report`). Update OpenCTI only when the user or the assignment asks for it: description and aliases with `update_opencti_field`, new `uses` / `targets` relationships with `create_opencti_relationship`, an analytic Note with `create_opencti_note`; or prepare the changes in a draft (`create_opencti_draft`) for review.

## Profile Template

```markdown
## Threat Actor Profile: [Primary Name]

### Summary
| Field | Value |
|-------|-------|
| **Primary Name** | [Most commonly used name] |
| **Aliases** | [All known aliases across vendors] |
| **Attribution** | [State sponsor / Criminal group / Hacktivist - with confidence level] |
| **Affiliation** | [Specific agency/unit if known, e.g., GRU Unit 26165] |
| **Motivation** | [Espionage / Financial / Disruption / Hacktivism / Mixed] |
| **Active Since** | [Year of first known activity] |
| **Status** | [Active / Dormant / Disbanded] |
| **Sophistication** | [Low / Medium / High / Advanced] |
| **Primary Targets** | [Sectors and geographies] |
| **Assessment Date** | [Date of this profile version] |

### Attribution Assessment
[Detailed attribution discussion with confidence level. Who attributes this group and based on what evidence? Where do vendors disagree?]

### Targeting
**Sectors**: [List targeted sectors with evidence]
**Geographies**: [Targeted countries/regions]
**Selection criteria**: [How does this group choose targets? Opportunistic vs targeted?]
**Evolution**: [How has targeting changed over time?]

### TTPs (MITRE ATT&CK)
| Tactic | Technique | ID | Notes |
|--------|-----------|-----|-------|
| Initial Access | Spearphishing Attachment | T1566.001 | Primary delivery method |
| Execution | PowerShell | T1059.001 | Used for download and execute |
| ... | ... | ... | ... |

### Tooling
| Tool | Type | Custom/Commodity | First Seen | Status | Notes |
|------|------|-----------------|-----------|--------|-------|
| [Tool 1] | Backdoor | Custom | 2024 | Active | Primary implant |
| Cobalt Strike | C2 | Commodity | 2023 | Active | Used with modified profiles |

### Infrastructure Patterns
- **Hosting preferences**: [Cloud providers, bulletproof hosting, compromised infrastructure]
- **Registrars**: [Preferred domain registrars]
- **TLS patterns**: [Self-signed? Let's Encrypt? Specific CAs?]
- **C2 protocols**: [HTTP/HTTPS, DNS, custom protocols]
- **IP ranges**: [Known IP blocks or ASNs]

### Campaign History
#### [Campaign 1] (YYYY-MM to YYYY-MM)
- **Targets**: [Who was targeted]
- **TTPs**: [Specific techniques used]
- **Outcome**: [Impact, attribution confidence]
- **References**: [Vendor reports]

### Relationships
- **Related groups**: [Parent/child groups, shared infrastructure, shared tools]
- **Overlap with**: [Groups that share TTPs or infrastructure]
- **Distinction from**: [Commonly confused groups and how to distinguish]

### Intelligence Gaps
- [What we don't know and need to collect]
- [Specific questions for future collection]

### Sources
| Date | Source | Reliability | Key Contribution |
|------|--------|-------------|-----------------|
| YYYY-MM-DD | [Source] | [Admiralty] | [What this source contributed to the profile] |
```

## Alias Mapping
Different vendors use different names for the same group:

| Vendor | Naming Convention | Example |
|--------|------------------|---------|
| Microsoft | Weather + element | Forest Blizzard |
| CrowdStrike | Animal + adjective | Fancy Bear |
| Mandiant/Google | APT + number | APT28 |
| MITRE | Group ID | G0007 |
| Kaspersky | Varies | Sofacy |
| Recorded Future | TAG-XX | TAG-110 |

Always list all known aliases and cross-reference when building profiles. Alias mappings between vendors are approximate: vendors cluster activity differently, so one vendor's group can overlap only partly with another's. Say so in the Relationships section ("overlaps with", "distinction from") rather than merging names silently. In OpenCTI, merge two intrusion sets (`merge_opencti_entities`) only when the user confirms they are the same cluster.

## Analytic Rigor (inline)

- **Attribution carries a confidence level**: Very High (90-100), High (75-89), Moderate (50-74), Low (25-49), Very Low (0-24). State the evidence type (infrastructure overlap, code overlap, TTP consistency, government attribution, vendor consensus) and where vendors disagree. Load the **Confidence Levels** skill for the full guidance.
- **Forward-looking statements** ("likely to target X next") use the probability yardstick: Almost certain (>95%), Highly likely (75-95%), Likely (50-75%), Realistic possibility (25-50%), Unlikely (15-25%), Highly unlikely (5-15%), Remote (<5%).
- **Sources** carry an Admiralty rating (reliability A-F, credibility 1-6, e.g. B2). Several vendors repeating one original report count as one source.
- **TLP**: the profile takes the most restrictive TLP of the sources it uses (vendor portal content is often TLP:AMBER or licensed for internal use only).
- For contested attribution, apply the **Analysis of Competing Hypotheses** skill.

## Profile Maintenance
- Update when new campaigns are attributed
- Update when new tools are discovered
- Update when targeting patterns change
- Review quarterly for staleness (compare `updated_at` of the OpenCTI intrusion set and the date of the newest vendor report)
- Move concluded campaigns from Active to Historical
- Update intelligence gaps as gaps are filled or new ones identified
- Record the update date and what changed, in the OpenCTI Note or in agent memory (`store_memory`) when writes are not allowed

## Related Skills

Load at most one or two when needed:
- **Campaign Tracking** - per-campaign records that roll up into the profile
- **Indicator Pivoting** - build the infrastructure cluster
- **Dark Web Collection** - forum and channel footprint of the actor's known aliases
- **Intelligence Writing** - the shorter Threat Actor Profile product template for dissemination
- Knowledge cells the profile may feed into or draw on: **Ransomware Ecosystem**, **Initial Access Brokers**, **Infostealers**, **China Cyber Espionage**, **Russia Cyber Espionage**, **Iran Cyber Espionage**, **DPRK Cyber Operations**, **Hacktivism**
