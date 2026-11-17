# Horizon Scanning

Horizon scanning identifies emerging threats, opportunities, and developments that could impact the organisation's security posture in the medium to long term (6-24 months). It hunts weak signals before they materialise and turns them into scenarios and early warning indicators.

## Process

### 1. Define Scope

- Time horizon (6 months? 12 months? 24 months?)
- Focus areas (specific sectors, threat types, geographies)
- Stakeholder needs (what decisions will this inform?)

If the organisation's Priority Intelligence Requirements (PIRs) are in OpenCTI, start from them (`manage_opencti_pir` to read the PIRs, `query_opencti_pir_entities` to see what already matches them). Otherwise look for previously stated priorities in the agent's memory (`search_memory`), or ask the user.

### 2. Identify Weak Signals

Weak signals are early indicators of emerging trends. Sources:
- Academic research and conference papers (BlackHat, DEF CON, CCC, academic journals)
- Underground forum discussions (new tools, techniques being discussed)
- Patent filings and startup activity (indicators of new capabilities)
- Geopolitical developments (sanctions, conflicts, elections)
- Regulatory changes (new compliance requirements creating new attack surfaces)
- Technology adoption trends (new tech = new attack surface)

#### Collection with XTM One integrations

Use only the tools you actually have; when an integration is not connected, say which source was skipped instead of guessing its result, and list it under Collection Gaps.

| Signal source | Integration and tools |
|---------------|-----------------------|
| Emerging technology and market trends | Feedly: `get_feedly_emerging_trends` (topics gaining momentum, with growth stage Exploding / Surging / Growing and audience size Mainstream / Known / Niche; pick the period that matches the horizon) |
| What is trending now (vulnerabilities, actors, new malware) | Feedly: `get_feedly_trending_threats` (kind `vulnerabilities`, `threat_actors` or `new_malware`); then `search_feedly_articles` / `get_feedly_article` to read the underlying reporting, `ask_feedly_ai` for a synthesis, `query_feedly_cyberattacks` for attack reporting |
| Your own knowledge base and its evolution | OpenCTI: `search_opencti`, `list_opencti_entities` (recent intrusion sets, malware, campaigns, reports), `get_opencti_entity_stats` and `list_opencti_relationships` to see what is gaining activity |
| News, regulation, geopolitics | NewsAPI: `search_newsapi_articles`, `get_newsapi_headlines`; RSS: `list_rss_feeds`, `fetch_rss_feeds`, `search_rss_entries` (vendor blogs, CERT advisories, regulators) |
| Research, conference papers, patents, startups | `search_google_web` then `web_fetch` (or `browser`) on the primary source |
| Vulnerability and exploitation trends | Vulnerability Intelligence: `list_vulnintel_recent_cves`, `list_vulnintel_top_epss`, `search_vulnintel_kev`; VulnCheck: `get_vulncheck_kev`, `get_vulncheck_exploits` |
| Underground economy and forum discussion | Intel 471: `search_intel471_forum_posts`, `search_intel471_marketplaces`; Flashpoint: `search_flashpoint_communities`; Cybersixgill: `search_cybersixgill_intel_items`; SOCRadar: `search_socradar_dark_web` |
| Ransomware business model evolution | Ransomware.live: `get_ransomlive_stats`, `list_ransomlive_recent_victims`, `list_ransomlive_groups` |
| Vendor strategic reporting | Google Threat Intelligence: `search_gti_threats`; Recorded Future: `search_rf_analyst_notes`; CrowdStrike: `list_crowdstrike_intel_reports` |
| AI-augmented attacks and attacks on AI systems | MITRE ATLAS: `search_atlas`, `list_atlas_case_studies` |

Rate every signal's source with the Admiralty Scale (reliability A-F, credibility 1-6). A single forum post is a weak signal (typically D4 or F6), not a trend. For the full scale, load the **Source Assessment (Admiralty Scale)** skill if it is attached.

### 3. Categorise Signals

| Category | Examples |
|----------|---------|
| **Emerging TTPs** | New exploitation techniques, novel social engineering methods, AI-augmented attacks |
| **Technology shifts** | New platforms widely adopted, legacy tech being deprecated, cloud migration patterns |
| **Threat actor evolution** | New groups emerging, existing groups changing targets, capability development |
| **Geopolitical drivers** | Conflicts, sanctions, elections, diplomatic shifts |
| **Regulatory/legal** | New laws, enforcement actions, liability changes |
| **Underground economy** | New services, market shifts, ecosystem changes |

### 4. Develop Scenarios

For each significant signal, develop three scenarios:
- **Best case**: signal does not materialise or is mitigated
- **Worst case**: signal materialises with maximum impact
- **Most likely**: balanced assessment based on available evidence

This also covers What-If and High-Impact / Low-Probability analysis: when a worst case is catastrophic, keep it in the report even if its likelihood is remote.

### 5. Assess Impact and Likelihood

For each scenario:
- Likelihood, using the probability yardstick: almost certain (>95%), highly likely (75-95%), likely (50-75%), realistic possibility (25-50%), unlikely (15-25%), highly unlikely (5-15%), remote (<5%). Always include the range. For the full rules, load the **Likelihood Language** skill if it is attached.
- Impact on organisation (Critical/High/Moderate/Low/Negligible)
- Time to materialise
- Confidence in assessment (Very High / High / Moderate / Low / Very Low, with a 0-100 score and rationale)

### 6. Identify Early Warning Indicators

For each high-impact scenario, define:
- What observable indicators would suggest this is materialising?
- Where would we see these indicators? (Collection sources: name the integration and tool from the table above.)
- How frequently should we monitor?

Indicators that should be watched continuously are a good fit for a recurring assignment on the agent. If the user asks, record the scan's signals and indicators in the agent's memory (`store_memory`) so the next scan can compare against this baseline (`search_memory`), or in OpenCTI as a Note or Report.

## Output Template

```markdown
## Horizon Scanning Report: [Focus Area]
**Period**: [Time horizon]
**Date**: YYYY-MM-DD

### Executive Summary
[Key emerging threats and their implications]

### Emerging Threats

#### [Threat 1]: [Title]
- **Signal strength**: Weak / Emerging / Established
- **Time horizon**: [When could this materialise?]
- **Likelihood**: [Probability yardstick term and range]
- **Potential impact**: [Critical/High/Moderate/Low]
- **Confidence**: [Level with rationale]
- **Description**: [What is this threat and why does it matter?]
- **Early warning indicators**: [What to watch for]
- **Recommended action**: [Proactive steps]

### Scenario Analysis
[For top 2-3 threats, develop best/worst/most likely scenarios]

### Collection Gaps
[What we need to monitor but currently can't, including integrations that were not connected]

### Sources
[Each source with its Admiralty rating]
```

Give the report a TLP marking (strategic landscape reports default to TLP:GREEN). When the user asks for a document, produce it with `generate_report`.

## Common Signals to Monitor (CTI)

- AI-powered phishing/deepfakes maturation
- Quantum computing impact on cryptography
- Supply chain security tooling gaps
- Cloud-native attack technique evolution
- Ransomware business model evolution
- Nation-state cyber capability proliferation
- Infostealer-to-ransomware pipeline evolution
- Edge device/IoT exploitation trends
