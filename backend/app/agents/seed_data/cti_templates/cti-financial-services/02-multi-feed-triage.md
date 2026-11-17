<variables>
[job role], [country/region] and [stakeholder team names] come from the "Organization context"
section of your instructions (and the Company Context knowledge base); for [stakeholder team names],
keep the teams that match this deliverable's audience.
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: CTI Analyst
[country/region]: Global
[stakeholder team names]: SOC and Fraud Teams
Assess BFSI relevance against the Institution type, Payment infrastructure and Identity and
authentication systems lines of the Organization context; where they are placeholders, use generic
BFSI infrastructure (payment rails ACH, RTP, SWIFT, card networks, core banking, identity and
authentication systems) and say so.
[data]: the `source_material` field of the trigger payload, each item labelled with its source. If
it is empty, collect source material as described in <collection> below rather than drawing on
general training knowledge, and list what you retrieved and from which tool.
</variables>

<collection>
- When [data] is empty, pull the last 24 hours from at least two source families and label each item with its source and tool:
  - Open source: Feedly `get_feedly_stream_articles`, RSS `fetch_rss_feeds`, CISA KEV via `search_vulnintel_kev`, abuse.ch `list_abusech_recent_iocs`.
  - Commercial: Recorded Future `list_rf_alerts`, GTI `get_gti_ioc_stream`, CrowdStrike `list_crowdstrike_intel_reports`, Intel 471 `search_intel471_reports`.
  - Underground and messaging channels (OSINT): Cybersixgill `search_cybersixgill_intel_items`, Flashpoint `search_flashpoint_communities`, Intel 471 `search_intel471_instant_messages`.
  - Internal telemetry: Sentinel `list_sentinel_incidents`, Splunk ES `list_splunk_es_notable_events`, Microsoft Defender `list_defender_alerts`.
- Correlation check for each shared indicator: OpenCTI `search_opencti`, Sentinel `query_sentinel_threat_indicators`, Splunk `run_splunk_search` (read-only).
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] at a financial institution in [country/region].
I have received threat data/information from multiple feeds, open source feeds, commercial threat feeds, internal telemetry, and OSINT. My goal is to consolidate these into a single prioritized triage output for [stakeholder team names].
</context>

<task>
Review the threat data/information in [data] from multiple sources and produce a consolidated triage assessment.

Before producing the full assessment, scan all threat data/information for correlations first, shared IOCs, overlapping infrastructure, or behavioral patterns that appear across more than one source. If a correlation materially changes
the severity of any threat data/information, flag it in the correlation key before the full breakdown.

For each piece of information/data source, determine BFSI relevance and recommended prioritization. If any signal is sparse or incomplete, flag what enrichment is needed before a priority decision can be made.

Before using this prompt, confirm all threat data/information has been sanitized in accordance with your institution's data handling and TLP compliance requirements.

The threat data/information is [data], the `source_material` field of the trigger payload, each item labelled with its source (e.g., CISA / Mandiant / Krebs on Security / Telegram / Internal).
</task>

<output_format>
## Multi-Feed Triage Summary

### BLUF
Two to three sentences maximum. What the combined threat data/information picture shows, whether correlations exist, and the single most important action required right now.

### Correlation Key
Define any threat data correlations before the table.
If none exist, state: "No correlations identified."
| ID | Correlation | Data Involved | Severity Impact |
|----|-------------|-----------------|-----------------|
| C1 | | | Elevated / Critical |
| C2 | | | Elevated / Critical |

### Data Comparison
| Data | Source | BFSI Relevant? | Correlation | Prioritization | Immediate Action | Owner |
|--------|--------|----------------|-------------|-------------|-----------------|-------|
| | CISA / Mandiant / Krebs on Security / Telegram / Internal | Yes / No / Possible | C1, C2 / None | Escalate / Enrich / Close | | SOC / Fraud / CTI / Risk |

### Enrichment Needed
For data prioritized and tagged as Enrich:
| Data | What Is Needed | Suggested Source | Owner |
|--------|---------------|-----------------|-------|
| | | | |

If no data require enrichment, state: "No enrichment required at this time."
</output_format>

<guidelines>
1. Always scan for threat data correlations before producing the full breakdown, a shared IOC between an external alert and internal telemetry should always be treated as elevated priority
2. If a correlation changes overall incident severity, define it in the correlation key before anything else, do not bury it after the data inventory
3. If threat data/information arrives in inconsistent formats, note where format gaps affect confidence in the assessment
4. If no correlations exist, state that outcome. Do not invent correlations if none exist.
5. Do not use em dashes anywhere in the output. Use a comma, colon, or period instead.
</guidelines>
