# Campaign Tracking

A campaign is a coordinated set of malicious activities carried out by a threat actor against specific targets over a defined period. Use this skill to start or update a campaign record, to document a named campaign across time and victims, or when an investigation has surfaced a multi-incident cluster that warrants formal tracking. It provides the record template (timeline, attribution, victimology, attack chain, Diamond Model mapping, IOC clusters) and the lifecycle from active to historical.

Use only the tools you actually have; when an integration is not connected, say which source was skipped instead of guessing its result. Never write to OpenCTI or MISP unless the user or the assignment asks for it.

## Campaigns in OpenCTI

OpenCTI models a campaign natively, which makes it the natural home of the record:

| Campaign record element | OpenCTI representation |
|-------------------------|------------------------|
| Campaign ID / name, first and last observed, objective | Campaign entity (name, aliases, `first_seen`, `last_seen`, objective, description) |
| Attribution | `attributed-to` relationship Campaign -> Intrusion Set / Threat Actor (with confidence) |
| Victimology | `targets` relationships Campaign -> Sector, Country, Region, Organization |
| Attack chain / TTPs | `uses` relationships Campaign -> Attack Pattern (ATT&CK) |
| Capability | `uses` relationships Campaign -> Malware, Tool; `targets` -> Vulnerability |
| Infrastructure / IOC clusters | Indicators (`indicates` -> Campaign), Observables, Infrastructure entities (`uses` / `compromises`) |
| Timeline and sources | Reports containing the campaign, each with its publication date and author |
| Status | Last observed date, plus a label (for example `campaign-active` / `campaign-concluded`) if the team uses one |

### Reading an existing campaign
1. `search_opencti` on the campaign name and aliases (and on the actor, to list its campaigns).
2. `get_opencti_entity` for the Campaign fields.
3. `list_opencti_relationships` from the Campaign for attribution, targets, techniques, malware and indicators; `get_opencti_top_related` for the most connected entities.
4. Reports that contain it, for the timeline: `get_opencti_container_full` on each relevant report.
5. `get_opencti_entity_history` to see what changed since the last review.

### Recording a campaign (only on request)
- Prefer a draft for review: `create_opencti_draft`, then `use_opencti_draft` to work inside it.
- Create the Campaign with `create_opencti_entity`; link it with `create_opencti_relationship` (`attributed-to`, `targets`, `uses`); add indicators with `create_opencti_indicator` and observables with `create_opencti_observable`; group the evidence in a Report with `create_opencti_container` and `add_opencti_to_container`; set the TLP with `add_opencti_marking`.
- For a large cluster, build a STIX 2.1 bundle instead (the **STIX Bundle Authoring** skill, validated with `stix_bundle_validator`).

## Collection Sources

- **Vendor campaign reporting**: GTI (`search_gti_threats` for campaign collections, `get_gti_threat_timeline`, `get_gti_threat_related_objects`, `search_gti_threat_iocs`); CrowdStrike (`list_crowdstrike_intel_reports`, `list_crowdstrike_intel_indicators`); Recorded Future (`search_rf_analyst_notes`); Feedly (`query_feedly_cyberattacks`, `search_feedly_articles`, `get_feedly_stream_iocs`); Flashpoint (`search_flashpoint_reports`).
- **Ransomware campaigns**: Ransomware.live `get_ransomlive_group` (documented TTPs, leak-site infrastructure, exploited vulnerabilities), `get_ransomlive_iocs` and `get_ransomlive_yara` (per-group dumps), `search_ransomlive_victims` (victim timeline). Pair with the **Ransomware Ecosystem** knowledge cell.
- **Internal telemetry**: incidents and alerts in the SIEM/EDR the platform is connected to (Sentinel `list_sentinel_incidents`, Defender `list_defender_incidents`, CrowdStrike `list_crowdstrike_incidents`, Splunk `list_splunk_es_notable_events`); prior MISP events (`search_misp_events`).
- **ATT&CK**: verify every technique ID with `search_mitre_attack` / `get_mitre_attack_object` before it enters the attack chain.
- **Building the IOC cluster**: the **Indicator Pivoting** skill (multi-hop graph walk) or the **IOC Enrichment Workflow** skill (bulk enrichment of raw IOCs); for a single indicator, the **IP Investigation**, **Domain Investigation**, **Hash Investigation** or **URL Investigation** skills.

## Campaign Template

```markdown
## Campaign: [Campaign ID / Name]

### Overview
| Field | Value |
|-------|-------|
| **Campaign ID** | CAMP-YYYY-MM-XXX |
| **Name** | [Descriptive name if known] |
| **Status** | Active / Dormant / Concluded |
| **First observed** | YYYY-MM-DD |
| **Last observed** | YYYY-MM-DD |
| **Attribution** | [Actor - with confidence level] |
| **Motivation** | [Espionage / Financial / Destruction / Hacktivism] |

### Timeline
| Date | Event | Source |
|------|-------|--------|
| YYYY-MM-DD | Initial delivery emails sent | Internal telemetry |
| YYYY-MM-DD | First successful compromise | Vendor report (B2) |
| YYYY-MM-DD | Lateral movement detected | SOC alert |
| YYYY-MM-DD | Data exfiltration observed | Network forensics |

### Attribution
[Assessment of who is behind this campaign, with confidence level. Reference threat actor profile if available.]

### Victimology
- **Sectors targeted**: [List]
- **Geographies**: [Countries/regions]
- **Number of known victims**: [Count with confidence]
- **Selection criteria**: [How were targets chosen? Opportunistic vs targeted?]
- **Common characteristics**: [What do victims have in common?]

### Attack Chain (Kill Chain / ATT&CK)
| Phase | Technique (ATT&CK) | Details |
|-------|-------------------|---------|
| Reconnaissance | T1598 Phishing for Information | Targeted LinkedIn messages to identify employees |
| Initial Access | T1566.001 Spearphishing Attachment | Malicious Word doc with macro |
| Execution | T1059.001 PowerShell | Macro downloads PowerShell stager |
| Persistence | T1547.001 Registry Run Keys | Run key added for backdoor |
| C2 | T1071.001 Application Layer Protocol | HTTPS to legitimate cloud service |
| Exfiltration | T1567.002 Exfiltration to Cloud Storage | Data uploaded to attacker-controlled cloud |

### Diamond Model
| Vertex | Details |
|--------|---------|
| **Adversary** | [Threat actor / group - reference profile] |
| **Capability** | [Malware, exploits, tools, techniques used] |
| **Infrastructure** | [C2 servers, staging, delivery infrastructure, domains] |
| **Victim** | [Targeted organisations, sectors, systems] |

**Meta-features:**
- **Direction**: [Adversary-to-Victim / Victim-to-Adversary / Bidirectional]
- **Methodology**: [Attack phases and progression]
- **Resources**: [Level of investment observed]
- **Social-Political**: [Geopolitical context driving the campaign]
- **Technology**: [Technology landscape enabling the campaign]

### IOC Clusters
#### Delivery Infrastructure
| Type | Value | First Seen | Status |
|------|-------|-----------|--------|
| Domain | phishing.example[.]com | 2026-01-15 | Active |
| IP | 203.0.113[.]42 | 2026-01-15 | Active |

#### C2 Infrastructure
| Type | Value | First Seen | Status |
|------|-------|-----------|--------|
| Domain | c2.badactor[.]net | 2026-01-20 | Active |
| IP | 198.51.100[.]10 | 2026-01-20 | Active |

#### Malware
| Hash (SHA-256) | Name | Type | First Seen |
|----------------|------|------|-----------|
| abc123... | loader.dll | Loader | 2026-01-15 |

### TTP Evolution During Campaign
[How have the attackers adapted? Changed tools? Modified techniques? Responded to detection?]

### Detection Guidance
[The SIGMA/YARA/KQL rules created for this campaign, in the output or as OpenCTI Indicators with the matching pattern type when the user asked for them to be stored.]

### Intelligence Gaps
[What we still don't know about this campaign]

### Sources
| Date | Source | Reliability | Key Finding |
|------|--------|-------------|-------------|
```

Defang indicators in the human-readable record; keep them refanged only in machine-readable exports.

## Campaign Linking
Campaigns may be related. Document relationships:
- **Same infrastructure**: Shared C2, shared registrant
- **Same tooling**: Same malware family or builder
- **Same TTPs**: Identical techniques across campaigns
- **Same victimology**: Same sector/geography targeting
- **Temporal overlap**: Concurrent operations

In OpenCTI, a `related-to` relationship between two campaigns, with the linking evidence in its description and a confidence level, records the link. Shared TTPs alone are weak evidence of a common operator (many actors use the same techniques); weight shared infrastructure and custom tooling higher.

## Campaign Lifecycle Management
1. **Detection**: Initial indicators or vendor report triggers campaign tracking
2. **Active tracking**: Continuous collection, IOC updates, TTP documentation (a scheduled assignment that re-reads the campaign and its sources fits well)
3. **Analysis**: Attribution, Diamond Model mapping, impact assessment
4. **Reporting**: Campaign report produced per the **Intelligence Writing** templates (`generate_report` for a printable product)
5. **Conclusion**: Campaign ends (dormant or concluded), move to historical (update `last_seen` and the status)
6. **Knowledge update**: Feed findings into the actor profile (the **Threat Actor Profiling** skill) and the relevant knowledge cell context; record them in OpenCTI or agent memory (`store_memory`) since built-in knowledge-cell skills are read-only

## Sharing the Campaign
- MISP: `create_misp_event` with the cluster as attributes (`add_misp_attribute`), TLP and galaxy tags (`add_misp_tag`); publish (`publish_misp_event`) only on explicit request.
- STIX 2.1: the **STIX Bundle Authoring** skill and `stix_bundle_validator`.
- OpenCTI: the Report container described above.
- The **Intelligence Sharing** skill covers TLP-governed dissemination.

## Analytic Rigor (inline)
- Attribution and victim counts carry a confidence level: Very High (90-100), High (75-89), Moderate (50-74), Low (25-49), Very Low (0-24).
- Forward-looking statements use the probability yardstick: Almost certain (>95%), Highly likely (75-95%), Likely (50-75%), Realistic possibility (25-50%), Unlikely (15-25%), Highly unlikely (5-15%), Remote (<5%).
- Each source carries an Admiralty rating (reliability A-F, credibility 1-6).
- The record carries a TLP no lower than its most restrictive source.
- Load the **Source Assessment (Admiralty Scale)** or **Confidence Levels** skill for the full guidance when needed.
