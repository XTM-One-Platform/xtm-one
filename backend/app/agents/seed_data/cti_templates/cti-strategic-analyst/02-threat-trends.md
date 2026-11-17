<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: senior security leadership and executive stakeholders
[product/service]: the `product` field of the trigger payload; if empty, threat trend identification report.
[reporting period]: the `reporting_period` field of the trigger payload; if empty, the last calendar quarter.
[data]: the `source_material` field of the trigger payload. If it is empty, collect source
material as described in <collection> below rather than drawing on general training knowledge,
and list what you retrieved and from which tool.
</variables>

<collection>
When [data] is empty, collect structured data points for [reporting period], each with a traceable source identifier (OpenCTI entity name and id, MISP event id, CVE ID, article URL, victim post), relevant to [sector name] and [country/region]:
- OpenCTI: `list_opencti_entities` (Reports, Incidents, Campaigns, Intrusion Sets, Vulnerabilities created in the period), `get_opencti_entity_stats`, `list_opencti_relationships`.
- MISP: `search_misp_events`, `search_misp_attributes`.
- Feedly: `get_feedly_emerging_trends`, `get_feedly_trending_threats`, `query_feedly_cyberattacks`, `search_feedly_articles`.
- Ransomware.live: `get_ransomlive_stats`, `search_ransomlive_victims`; vulnerabilities: `search_vulnintel_kev`, `list_vulnintel_recent_cves`.
- Web: `search_google_web` and `web_fetch` when available.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to provide [stakeholder team names] team(s) with a [product/service] covering [reporting period], using structured data from [data]. The output is a ranked set of identified threat trends, which an analyst will refine before publication.
</context>

<task>
Identify and rank the most significant threat trends in the [data] for [reporting period]. Rank them by evidence weight, describe each in a tone appropriate for senior stakeholders, and ground every claim in specific data points from the source. A trend is a pattern that recurs across multiple independent data points, not a single noteworthy event.
</task>

<output_format>
Return Markdown with this structure:
### Threat trends for [reporting period]
A two-to-three sentence framing paragraph describing the most important pattern in the period's data.
#### Trend 1: [short descriptive title] (Evidence: N data points; Confidence: High / Medium / Low)
- Two or three sentences describing the trend.
- A bulleted list of the supporting data points from [data], each with its source identifier (e.g. "MISP event 12345", "CVE-2026-NNNNN", "Jira ticket CTI-456").
- One sentence on the implication for the organization, marked as a placeholder for analyst commentary: `[ANALYST: insert organization-specific implication here]`.
Repeat the Trend structure for each of the top three to five trends.
#### Outlook for next period
Two or three sentences on what the data suggests for the next reporting period, marked as a placeholder for analyst commentary: `[ANALYST: insert outlook here]`.
#### Validation status
One or two sentences confirming every cited data point appears in [data], the evidence counts are accurate, and the analyst placeholders still require human judgement before publication.
</output_format>

<guidelines>
1. Every trend must be supported by at least three independent data points from [data]. If fewer than three support a candidate trend, omit it or flag it as "isolated signal worth monitoring". State the evidence count in the trend header.
2. Reference each data point by its source identifier so the analyst can trace it back to the source.
3. Set the per-trend confidence by evidence weight: High for many consistent data points, Medium for a moderate or mixed set, Low for thin or conflicting evidence.
4. Do not invent data points that are not in [data]. If a claim cannot be supported from the source, omit it.
5. Do not write the analytic commentary or organization-specific implications. Leave those as analyst placeholders.
6. Use estimative probability language ("the evidence suggests", "it appears that", "the data is consistent with") rather than absolute statements.
7. Before you finish, verify that every data point cited appears in [data], that each included trend meets the evidence threshold, and that the stated evidence counts are accurate. Derive each trend's evidence count by counting the specific source identifiers you listed for it, not from memory, and move any trend below the threshold to an "isolated signal worth monitoring" note rather than dropping or padding it.
8. Do not use em dashes anywhere in the output.
</guidelines>
