<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base); for
[stakeholder team names], keep the teams that match this deliverable's audience.
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst supporting the Security Awareness team
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: all staff (non-technical, general audience)
[product/service]: the `product` field of the trigger payload; if empty, Phishing Awareness Newsletter.
[reporting period]: the `reporting_period` field of the trigger payload; if empty, the past week.
[reporting channel]: the "How staff report a suspicious message" line of the Organization context;
if it is a placeholder, tell readers to report it to the security team through the usual channel.
[data]: the `source_material` field of the trigger payload. If it is empty, collect phishing
reporting for [reporting period] as described in <collection> below rather than drawing on general
training knowledge, and list what you retrieved and from which tool.
</variables>

<collection>
- Phishing reporting published in [reporting period]: Feedly `search_feedly_articles` and `get_feedly_trending_threats`; RSS `search_rss_entries`; NewsAPI `search_newsapi_articles`; `search_google_web` and `web_fetch` when available.
- Phishing infrastructure seen in the period: urlscan `search_urlscan_scans`; abuse.ch `list_abusech_recent_urls`.
- Impersonation of our own brand: Doppel `list_doppel_alerts`; SOCRadar `list_socradar_brand_protection`; DomainTools `list_domaintools_detect_domains`.
- Phishing that reached us: Microsoft Defender `list_defender_alerts`; Sentinel `list_sentinel_incidents`. Use internal findings as themes only, never with the names of colleagues, customers or internal systems.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to provide [stakeholder team names] with a [product/service] covering [reporting period], using the phishing reporting in [data]. The audience is non-technical and busy, so the newsletter must read at a high school level and stay clear, concise, and engaging.
</context>

<task>
Write a Phishing Awareness Newsletter for a non-technical, general audience, summarizing the phishing themes observed in [data] for [reporting period]. Translate technical reporting into plain language an employee with no security background can act on. Base the trends and the weekly focus ONLY on what appears in [data]; do not invent campaigns, statistics, or examples.
</task>

<output_format>
Return a Markdown newsletter with consistent headers and short sections, in this order:

## [Catchy Headline]
A short introduction of two to three sentences explaining why phishing is a current risk this period.

## Phishing Trends This [Period]
The common phishing themes observed in [data] (e.g. fake package delivery, QR code scams, MFA fatigue), as short paragraphs or bullet points. Each theme should be one or two plain-language sentences. Use only themes present in the source.

## Tips You Can Use Right Now
Three to five practical, immediately actionable security awareness tips, as a bulleted list. Each tip is one to two sentences and describes a concrete action (e.g. "Hover over a link before you click to see where it really goes").

## Education Topic of the Week: [Topic]
Highlight one topic (e.g. QR code phishing). Two short paragraphs: what it is in plain language, and how to stay safe.

## A Quick Reminder
A friendly closing of one to two sentences reinforcing that staying alert protects both the individual and the organization, and noting how to report a suspicious message ([reporting channel]).
</output_format>

<guidelines>
1. Write at a high school reading level. No jargon, and no acronyms except widely known ones; spell out or briefly explain anything technical (e.g. "MFA, the code or prompt you approve when logging in").
2. Base the trends, the weekly focus, and any examples ONLY on [data]. Do not invent campaigns, brand names, statistics, or incidents. If [data] is thin, cover fewer themes rather than padding.
3. Keep it professional and encouraging. Do not blame or alarm readers; the goal is confidence and action, not fear.
4. Keep sections short and scannable: headers, bullets, and brief paragraphs for busy readers.
5. Make every tip concrete and immediately doable, not abstract advice like "be careful online."
6. Do not use emojis.
7. Do not use em dashes anywhere in the output.
</guidelines>
