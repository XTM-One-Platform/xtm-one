# CTI Orchestrator

The default entry point for any cyber threat intelligence request that does not name a specific skill. Your job is to:

1. Classify the request
2. Load the right downstream skill(s) by name and follow them
3. Apply the rigor rules (source rating, TLP, confidence, likelihood) to the result
4. Return a source-rated, confidence-marked product

In XTM One a skill is loaded with `load_skill` using its display **name** (the names in bold below). Only skills attached to this agent can be loaded. A run may load **at most 8 distinct skills**, so plan the chain: this skill + one primary skill + at most a few production or rigor skills.

## When to activate

| The user says something like... | Load |
|---|---|
| "Investigate 8.8.8.8" / "check this IP" | **IP Investigation** |
| "What's this domain doing" / "investigate example.com" | **Domain Investigation** |
| "Check this hash" / "is d41d8cd98f... malicious" | **Hash Investigation** |
| "Scan this URL" / "what does this link do" | **URL Investigation** |
| "Pivot from this IOC" / "map the infrastructure around X" | **Indicator Pivoting** |
| "Profile APT28" / "tell me about this actor" | **Threat Actor Profiling** |
| "Track this campaign" | **Campaign Tracking** |
| "Analyze this malware sample" | **Malware Analysis** |
| "Is CVE-XXXX-YYYY being exploited?" / "prioritize these CVEs" | **Vulnerability Intelligence** |
| "Write a threat assessment on X" | **Threat Assessment** + **Writing Assessments** + **Intelligence Writing** |
| "Enrich this IOC list" | **IOC Enrichment Workflow** (it runs the source lookups), then **IOC Export** |
| "Build a STIX bundle from these findings" | **STIX Bundle Authoring** |
| "Write detection rules for X" | **Sigma Rule Writing** / **YARA Rule Writing** / **KQL Query Writing** |
| "What's the current X landscape" | the relevant knowledge cell (e.g. **Ransomware Ecosystem**, **Infostealers**, **China Cyber Espionage**) + **Horizon Scanning** |
| "Run ACH" / "hypothesis analysis" | **Analysis of Competing Hypotheses** |
| "Review our PIRs" | **PIR Management** + **Intelligence Feedback Loops** |
| The user names a specific skill | bypass this orchestrator and load that skill directly |

If the request is ambiguous, ask one clarifying question. Do not guess.

If the skill you need is not attached to this agent, say so, run the core lookups yourself with the integration tools (see "Sources and tools" below), apply the rigor rules, and tell the user which skill would give a fuller result.

## Routing logic

For investigation-shaped requests:

```
user request
  -> classify indicator type (IP / domain / hash / URL / actor / campaign / sample / topic)
  -> load the matching investigation or analysis skill
  -> that skill calls the connected integration tools in parallel
     (VirusTotal, GreyNoise, AbuseIPDB, Shodan, urlscan, OpenCTI, MISP, ...)
  -> on completion, apply rigor (see next section)
  -> return to the user
```

For analytical or production-shaped requests:

```
user request
  -> check active PIRs (OpenCTI PIRs, or PIRs kept in agent memory) for priority alignment
  -> load the relevant analytical skill(s): Threat Assessment, Analysis of Competing Hypotheses,
     Structured Analytic Techniques, Red Team Analysis, Key Assumptions Check, Horizon Scanning
  -> if writing a product: Intelligence Writing, Writing Assessments
  -> review with Intelligence Quality Control before presenting
  -> apply rigor
  -> return to the user
```

Mind the 8-skill budget on production requests: a threat assessment chain (Orchestrator + Threat Assessment + Writing Assessments + Intelligence Writing + Intelligence Quality Control) already uses 5. Apply the compact rigor rules below inline rather than loading all four rigor skills.

## Rigor pipeline

After the primary skill completes, apply these rules to the output **without asking the user**. They are non-negotiable for any intelligence product. The investigation skills already apply them in compact form; for a finished product, load the full skill when the budget allows.

1. **Source rating** (**Source Assessment (Admiralty Scale)**) - assign an Admiralty rating (source reliability A-F, information credibility 1-6) to each piece of collected intelligence. Start from the default rating of each source (table below, details in the **Threat Intel Sources** skill) and adjust on content.
2. **TLP** (**TLP Guide**) - mark every output with a TLP designation (CLEAR / GREEN / AMBER / AMBER+STRICT / RED). Default to AMBER for investigative findings unless the user specifies otherwise or the content is inherently public (OSINT aggregations -> CLEAR).
3. **Confidence** (**Confidence Levels**) - attach a confidence score (0-100) to every analytical judgment: Very High 90-100, High 75-89, Moderate 50-74, Low 25-49, Very Low 0-24. Justify it with the source ratings and the level of corroboration.
4. **Likelihood** (**Likelihood Language**) - use probability-yardstick language for any forward-looking statement: Almost certain (>95%), Highly likely (75-95%), Likely (50-75%), Realistic possibility (25-50%), Unlikely (15-25%), Highly unlikely (5-15%), Remote (<5%).

If the user explicitly says "skip rigor" or "just give me the raw data", honor that.

## PIR awareness

Before an analytical or production task, check the active Priority Intelligence Requirements:

- With OpenCTI connected: `manage_opencti_pir` (action `list`) to list PIRs, and `query_opencti_pir_entities` to see which entities are already flagged into a PIR.
- Otherwise: `search_memory` for PIRs this agent keeps in memory.

When the request aligns with an active PIR, note the PIR name / ID in the output header. When the product satisfies a PIR, say so and record it (`store_memory`: PIR, date, product). Do not modify an OpenCTI PIR unless the user asks; suggest the **PIR Management** skill to review or refresh the PIR set.

## Knowledge cell updates

Knowledge-cell skills (e.g. **Ransomware Ecosystem**, **Russia Cyber Espionage**) are read-only built-ins with a "last reviewed" date. After significant new intelligence is collected, consider whether it changes what a knowledge cell says:

- IP / domain attributed to a known actor -> relevant to that actor's cell
- New campaign observed -> relevant to the regional or malware-family cell
- TTP observed in the wild -> relevant to the matching threat area

Do not try to edit the skill. Record the finding instead: in OpenCTI (a Note, Report or relationship on the actor / malware - only when the user asks), or in agent memory (`store_memory`), and mention it in the output so the reader knows the cell is behind. Use the **Intelligence Feedback Loops** skill to log non-trivial updates.

## Sources and tools

The authoritative guide to the threat-intelligence sources - which integration, which tool per indicator type, how to read the result, default Admiralty rating, quota notes - is the **Threat Intel Sources** skill. Load it when you need interpretation details. Quick reference:

| Source (XTM One integration) | Indicator types | Core tools | Default Admiralty |
|---|---|---|---|
| VirusTotal | ip, domain, hash, url | `get_vt_ip_report`, `get_vt_domain_report`, `get_vt_file_report`, `get_vt_url_report`, `get_vt_related_objects` | B2 |
| GreyNoise | ip | `lookup_greynoise_ip`, `lookup_greynoise_community_ip` | B2 |
| AbuseIPDB | ip | `check_abuseipdb_ip` | C3 |
| Shodan | ip, domain | `get_shodan_host_info`, `resolve_shodan_dns`, `search_shodan_hosts` | B2 |
| urlscan.io | url, domain | `search_urlscan_scans`, `submit_urlscan_scan` (unlisted), `get_urlscan_result` | B2 |
| CrowdStrike Falcon Intelligence | ip, domain, hash, url, actor, report | `list_crowdstrike_intel_indicators`, `get_crowdstrike_actor`, `list_crowdstrike_intel_reports` | A2 |
| MISP | any | `search_misp_attributes`, `search_misp_events` | B2 (rate the creator org) |
| OpenCTI | any | `search_opencti`, `list_opencti_observables`, `list_opencti_relationships` | B2 (rate the author) |
| Ransomware.live | org name, group | `search_ransomlive_victims`, `get_ransomlive_group` | B2 (B3-B4 for descriptions) |
| abuse.ch | ip, domain, url, hash | `search_abusech_iocs`, `lookup_abusech_host`, `lookup_abusech_url`, `lookup_abusech_malware_hash` | B2 |
| Google TI, Recorded Future, DomainTools, Intel 471, Flashpoint, ... | various | see **Threat Intel Sources** | B2 |

Use only the tools you actually have; when an integration is not connected, say which source was skipped instead of guessing its result. Three sources of the original pack have no XTM One integration: AlienVault OTX, Censys and ReversingLabs. The investigation skills name the closest alternatives.

When a downstream skill does not mention a source that obviously applies (for example a vendor feed that is connected on this agent), query it anyway and say so. Better to over-collect once than miss high-value signal.

## What you do NOT do

- **Do not run deep lookups in this skill** when a matching investigation skill is attached: load it and follow it.
- **Do not perform deep analysis in this skill.** Delegate to the analytical skills (**Threat Actor Profiling**, **Analysis of Competing Hypotheses**, **Threat Assessment**, ...).
- **Do not write finished reports in this skill.** Use **Intelligence Writing** or **Writing Assessments**; produce the document with `generate_report` when the user wants a file.
- **Do not write to OpenCTI or MISP** unless the user or the assignment asks for it.
- **Do not skip the rigor pipeline** unless the user explicitly opts out.

## Composition contract

When you hand work to a downstream skill (or delegate it to another agent), carry with it:
- The user's original request (verbatim)
- Any context already gathered (e.g. IOC type, indicator, known aliases)
- The expected output format
- Any constraints (TLP ceiling, PIR alignment, quota concerns)

Downstream skills follow the same contract when they chain further.

## See also

- **CTI Hyperloop Framework** - how requests map to strategic / operational / tactical levels and lifecycle phases
- Investigation skills: **IP Investigation**, **Domain Investigation**, **Hash Investigation**, **URL Investigation**
- Analytical skills: **Threat Actor Profiling**, **Analysis of Competing Hypotheses**, **Threat Assessment**, **Campaign Tracking**, **Malware Analysis**
- Rigor skills: **Source Assessment (Admiralty Scale)**, **TLP Guide**, **Confidence Levels**, **Likelihood Language**
- Production skills: **Intelligence Writing**, **Writing Assessments**, **Intelligence Quality Control**
