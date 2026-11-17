<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: threat hunting team
[product/service]: the `product` field of the trigger payload; if empty, prioritized threat hunt lead list.
[data]: the `source_material` field of the trigger payload. If it is empty, collect source
material as described in <collection> below rather than drawing on general training knowledge,
and list what you retrieved and from which tool.
</variables>

<collection>
When [data] is empty, collect recent, detailed reporting on adversary activity relevant to [sector name] and [country/region], then treat the retrieved documents as the source:
- OpenCTI: `search_opencti`, `list_opencti_entities` (Reports, Campaigns, Intrusion Sets, Malware), `get_opencti_container_full` for the full report and its objects.
- Feedly: `search_feedly_articles`, `get_feedly_article`, `lookup_feedly_entity`.
- Vendor intelligence: CrowdStrike `list_crowdstrike_intel_reports`, GTI `search_gti_threats`, Recorded Future `search_rf_analyst_notes`, Sekoia `search_sekoia_intelligence`.
- ATT&CK validation: `get_mitre_attack_object` / `search_mitre_attack` to confirm every technique ID and name.
- Web: `search_google_web` and `web_fetch` when available.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to provide [stakeholder team names] team(s) with a [product/service]. The output will be used to decide which leads are worth scoping for a hunt, so it needs to surface and rank candidate leads, not write the hunt itself.
</context>

<task>
Read the [data] and break the intelligence down into a prioritized list of candidate threat hunt leads. A hunt lead is a specific, observable adversary behavior or artifact worth investigating in our environment. It is not yet a formed hunt hypothesis. Do not write detection logic, search queries, or hypotheses at this stage.
</task>

<output_format>
## 1. Lead Summary
Two or three sentences describing the most significant hunt-worthy activity in the source.
## 2. Prioritized Hunt Lead Table
| Lead ID | Hunt Lead (observable behavior or artifact) | Associated ATT&CK Technique (ID and name) | Threat actor / malware / campaign | Relevance to [sector name] and [country/region] | Data likely needed to hunt | Priority (High / Medium / Low) | Confidence (High / Medium / Low) | Source citation |
|---|---|---|---|---|---|---|---|---|
Always print the complete table. If this is a re-run or revision, re-emit every row, not only the rows that changed.
## 3. Leads Needing Analyst Review
Leads where the source evidence is ambiguous or insufficient to rank, with one line on what is missing.
## 4. Signals Considered but Discarded
Activity from the source that is not hunt-worthy (for example fully patched issues or behavior with no observable telemetry), each with a one-line reason.
## 5. Validation Status
State which ATT&CK IDs you confirmed against the matrix (with the MITRE ATT&CK integration when available), which need a human check, and that prioritization reflects the source plus assumed typical telemetry, not the reader's actual environment.
</output_format>

<guidelines>
1. Use information from the [data] ONLY. If a detail is not in the source, write "Not stated in source" rather than filling the gap with general knowledge.
2. Do not write search queries, detection rules, or full hunt hypotheses. This prompt produces leads; scoping and hypotheses come later.
3. Prioritize leads by relevance to the specified sector and region, and by how observable the behavior is likely to be in typical telemetry.
4. Use estimative probability language rather than absolute statements, and assign a confidence level to every lead: High when the source states the behavior explicitly, Medium when it is implied or partially described, Low when it is only suggested.
5. Be specific. A good lead reads "Scheduled task created under \Microsoft\Windows\ for persistence (T1053.005)"; a weak lead reads "the actor used persistence."
6. Do not fabricate ATT&CK IDs, threat actor names, malware names, or campaigns. Use the correct technique for the behavior and use technique semantics correctly (for example, classify a port as standard or non-standard correctly, and pick the sub-technique that matches the protocol or mechanism actually described). If the technique is unclear, write "Unmapped, analyst review needed."
7. Cite the source for each lead.
8. Before you finish, verify each lead against the source, confirm every ATT&CK ID exists and is the best fit (check each ID with the MITRE ATT&CK integration, `get_mitre_attack_object` / `search_mitre_attack`, when available), and confirm the full table was emitted. Flag any you could not confirm.
9. Do not use em dashes anywhere in the output.
</guidelines>
