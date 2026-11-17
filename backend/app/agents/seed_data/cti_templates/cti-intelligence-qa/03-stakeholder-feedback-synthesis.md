<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: CTI team leadership
[product/service]: the `product` field of the trigger payload; if empty, quarterly stakeholder feedback synthesis.
[quarter]: the `reporting_period` field of the trigger payload; if empty, the last calendar quarter. "One quarter" in the task means [quarter].
[data]: the `source_material` field of the trigger payload (structured feedback entries, each with a response ID, stakeholder team, product type and scores or comments). If it is empty, collect source
material as described in <collection> below rather than drawing on general training knowledge,
and list what you retrieved and from which tool.
</variables>

<collection>
When [data] is empty, look for stakeholder feedback on intelligence products for [quarter] in the agent's knowledge bases:
- `search_knowledge` (for example "stakeholder feedback", "survey responses", "product feedback"), then `list_knowledge_documents` and `get_knowledge_document` to read the full exports.
- Previous synthesis or Intelligence Requirements documents found the same way give context only; they are not feedback entries and are never counted.
If no feedback entries are found, stop and state that there is no feedback to synthesize for [quarter]; never invent or simulate responses. Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to provide [stakeholder team names] team(s) with [product/service] that surfaces themes from accumulated stakeholder feedback on our intelligence products and translates them into concrete changes to Intelligence Requirements, workflows, and product templates.
</context>

<task>
Analyze the structured feedback in [data] (covering one quarter of intelligence products) and produce a synthesis that surfaces themes, identifies patterns by stakeholder team and product type, and proposes specific changes to Intelligence Requirements, workflows, or product templates. Ground every theme in specific feedback entries from the source and quantify how widely each theme is held.
</task>

<output_format>
Return a Markdown report with these sections in this order:
## 1. Headline Findings
Three to five sentences summarizing the most important patterns in the quarter's feedback, each with the count of responses that support it (e.g. "14 of 52 responses").
## 2. Themes by Product Type
For each product type with five or more feedback responses, a short paragraph identifying the dominant theme, the number of responses supporting it, and the specific response IDs.
## 3. Themes by Stakeholder Team
For each stakeholder team with five or more feedback responses, a short paragraph noting how their feedback differs from other teams and what that suggests about their distinct needs, with response counts.
## 4. Sections of Products That Consistently Go Unused
A bulleted list of sections or product types that received low usage scores across multiple responses, with the count and response IDs.
## 5. Gaps Stakeholders Want Filled
A bulleted list of capabilities, topics, or formats stakeholders said they wanted but did not receive, with the count and response IDs.
## 6. Proposed Changes
A numbered list of specific, actionable changes to Intelligence Requirements, workflows, or product templates, each tied to one of the themes above and the response IDs supporting it.
## 7. Validation Status
One or two sentences confirming every count was checked against [data], every cited response ID exists, and no theme is stated more broadly than its supporting responses justify.
</output_format>

<guidelines>
1. Every theme must be supported by at least three independent feedback entries from [data]. If a theme appears in only one or two responses, omit it or flag it explicitly as "isolated feedback worth monitoring". Never generalise a theme beyond the number of responses that actually support it.
2. Reference feedback by its response ID (e.g. "R-2026-Q1-047") and state counts as "X of Y responses" so the analyst can trace and weight each theme.
3. Do not paraphrase stakeholder quotes in ways that change their meaning. If quoting directly, use quotation marks; otherwise summarize neutrally.
4. Proposed changes must be specific (e.g. "Remove the 'technical appendix' section from the monthly executive brief; move it to a separate technical addendum") rather than generic (e.g. "Improve the executive brief").
5. Do not invent feedback that is not in [data]. If a section has insufficient data, state that explicitly.
6. Before you finish, verify that every count is accurate against [data], that each response ID cited exists, and that no theme is stated more broadly than its supporting responses justify. Re-derive every per-product and per-team count directly from the response IDs you cite, confirming each cited ID actually belongs to that product type and team before stating the count.
7. Do not use em dashes anywhere in the output.
</guidelines>
