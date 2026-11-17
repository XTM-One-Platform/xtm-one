<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base); for
[stakeholder team names], keep the teams that match this deliverable's audience.
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: senior leadership
[data]: the `source_material` field of the trigger payload (the validated analysis). If it is
empty, collect the existing analysis of the threat named in the `threat_focus` field as described
in <collection> below rather than drawing on general training knowledge, and list what you
retrieved and from which tool. If both fields are empty, say that a validated analysis or a threat
focus is needed and stop.
</variables>

<collection>
- Validated analysis already published on the threat: OpenCTI `search_opencti`, then `get_opencti_container_full` for the matching Report or Case; Feedly `search_feedly_articles` only to confirm what the threat is.
- Exposure context for the analyst to confirm: the Company Context knowledge base (`search_knowledge`); Microsoft Defender `list_defender_software` / `list_defender_vulnerabilities` when the threat targets a product.
- Prefer a finished, reviewed product over raw reporting, and say which one you used.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to tell [stakeholder team names], in plain language, that we are aware of a threat, what it means for us, and what we are doing.
</context>

<task>
Using the validated analysis in [data], draft a short awareness note for a non-technical audience.
</task>

<output_format>
Four short paragraphs, with headers and a brief title:
0. Title: Intelligence brief [insert suitable title, including name of threat]
1. What the threat is, in plain language.
2. Whether we appear exposed. [ANALYST CONFIRMS]
3. The likely business impact, in estimative terms.
4. What we are doing, and any decision we need from leadership.
</output_format>

<guidelines>
1. Keep the whole note under 150 words (excluding title and headers).
2. Use estimative language (likely, probable, possible). No fear-uncertainty-doubt framing and no absolute claims.
3. Do not include technical indicators or jargon; this is for a business audience.
4. Leave the exposure statement for the analyst to confirm; mark it [ANALYST CONFIRMS].
5. Base the note only on [data]; do not introduce new claims.
6. Add headers in bold for each section of the brief.
</guidelines>
