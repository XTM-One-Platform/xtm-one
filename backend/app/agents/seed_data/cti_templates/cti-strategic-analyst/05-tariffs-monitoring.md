<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: technical security stakeholders
[product/service]: the `product` field of the trigger payload; if empty, Threat intelligence report.
[tariff scope]: the `tariff_scope` field of the trigger payload; if empty, U.S. Tariffs.
[reporting period]: the `reporting_period` field of the trigger payload; if empty, last 30 days. "Recent news" means news published in [reporting period].
"My organization" in the output also reads the Operating countries and key supply-chain
dependencies line of the Organization context; if it is missing, assess by sector and region only and say so.
[data]: the `source_material` field of the trigger payload. If it is empty, collect source
material as described in <collection> below rather than drawing on general training knowledge,
and list what you retrieved and from which tool.
</variables>

<collection>
Whether or not [data] is supplied, the task asks for "any relevant current information": gather it for [tariff scope] in [reporting period], favouring official sources (government announcements, trade authority notices) over commentary:
- News: `search_newsapi_articles`, `get_newsapi_headlines`, `search_rss_entries`, Feedly `search_feedly_articles`.
- Web and official texts: `search_google_web` and `web_fetch` when available.
- Company knowledge: `search_knowledge` for supplier, vendor and operating-country details.
Give every tariff rate and effective date its source and publication date. Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to provide [stakeholder team names] team(s) with [product/service].
</context>

<task>
Generate an update on recent news about [tariff scope] using the [data] and any relevant current information.
</task>

<output_format>
## 1. Overview
Include an overview of the current tariff situation and recent developments.

## 2. Proposed Tariffs
| Parties Involved | Amount/Rate | Industries Impacted | Effective Date |
|------------------|-------------|---------------------|----------------|
| Country/Entity   | X%          | Industry names      | Date           |

## 3. Outlook
Provide the outlook going forward, including:
1. Expected timeline for implementation
2. Potential escalation or de-escalation scenarios
3. Impact on supply chains and operations

## 4. Conclusion
1. Key takeaways for my organization
2. Recommended monitoring indicators
3. Suggested preparatory actions
</output_format>

<guidelines>
1. Focus on tariffs relevant to the specified industry and region
2. Include both enacted and proposed tariffs
3. Provide context for how tariffs may impact supply chains and vendor relationships
4. Identify indicators to monitor for policy changes
5. Prioritize actionable intelligence for business decisions
6. Include citations to source material
</guidelines>
