<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: technical security stakeholders
[product/service]: the `product` field of the trigger payload; if empty, Threat intelligence report.
[Industry]: the same value as [sector name].
[Date Range]: the `date_range` field of the trigger payload; if empty, last 90 days.
[data]: the `source_material` field of the trigger payload. If it is empty, collect source
material as described in <collection> below rather than drawing on general training knowledge,
and list what you retrieved and from which tool.
</variables>

<collection>
When [data] is empty, collect reporting on the [Industry] sector in [country/region] for [Date Range]:
- OpenCTI: `search_opencti` and `list_opencti_entities` (Reports, Intrusion Sets, Campaigns, Incidents targeting the sector), `list_opencti_relationships` for actors and attack patterns, `get_opencti_entity_stats` for volumes.
- Feedly: `search_feedly_articles`, `query_feedly_cyberattacks`, `get_feedly_trending_threats`.
- Victimology: Ransomware.live `search_ransomlive_victims`, `list_ransomlive_sectors`, `get_ransomlive_stats`.
- Vendor intelligence: Recorded Future `search_rf_analyst_notes`, GTI `search_gti_threats`, CrowdStrike `list_crowdstrike_intel_reports`, Intel 471 `search_intel471_reports`.
- Incidents, case studies and regulation: `search_newsapi_articles`, `search_rss_entries`, `search_google_web` and `web_fetch` when available.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to provide [stakeholder team names] team(s) with [product/service].
</context>

<task>
Generate an Industry-specific Cybersecurity Report focused on the [Industry] sector using the [data].
</task>

<output_format>
## 1. Title
[Industry] Sector Cybersecurity Threat Report - [Date Range]

## 2. Executive Summary
High-level overview of the threat landscape for the sector.

## 3. Industry Threat Landscape
1. Active threat actors targeting the sector
2. Prevalent attack vectors
3. Trends and shifts in adversary behavior

## 4. Notable Incidents and Breaches (if mentioned)
1. Incident 1: Description, impact, lessons learned
2. Incident 2: Description, impact, lessons learned

## 5. Regulatory and Compliance Landscape (if mentioned)
1. Recent regulatory changes
2. Compliance implications
3. Enforcement trends

## 6. Attacker Motivations and Targets (if mentioned)
1. Primary motivations (financial, espionage, disruption)
2. High-value targets within the sector
3. Data and assets at risk

## 7. Industry-Specific Challenges (if mentioned)
1. Unique vulnerabilities
2. Legacy system risks
3. Supply chain considerations

## 8. Best Practices and Recommendations
1. Immediate actions
2. Short-term improvements
3. Long-term strategic initiatives

## 9. Case Studies (if mentioned)
1. Case study 1: Context, attack details, response, outcomes
2. Case study 2: Context, attack details, response, outcomes
</output_format>

<guidelines>
1. Focus on threats and incidents relevant to the specified industry
2. Include sector-specific regulatory and compliance context
3. Provide actionable best practices tailored to industry challenges
4. Balance technical detail with executive-level accessibility
5. Highlight trends that indicate future threat evolution
6. Include citations to source material
</guidelines>
