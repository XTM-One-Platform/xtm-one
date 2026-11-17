<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: technical security stakeholders
[write to OpenCTI]: the `write_to_opencti` field of the trigger payload; if empty, false.
[data]: the `source_material` field of the trigger payload (the report). If it is empty, collect
source material as described in <collection> below rather than drawing on general training
knowledge, and list what you retrieved and from which tool.
</variables>

<collection>
- The report itself: OpenCTI `search_opencti`, `get_opencti_container_full`; Feedly `get_feedly_article`, `get_feedly_stream_iocs`; `web_fetch` for a report URL.
- Enrichment (reputation, first seen, classification): VirusTotal `get_vt_file_report` / `get_vt_ip_report` / `get_vt_domain_report` / `get_vt_url_report`, GreyNoise `lookup_greynoise_ip`, AbuseIPDB `check_abuseipdb_ip`, Shodan `get_shodan_host_info` / `get_shodan_domain_info`, urlscan `search_urlscan_scans`, abuse.ch `get_abusech_ioc` / `lookup_abusech_malware_hash`, Recorded Future `enrich_rf_indicators`, GTI `get_gti_ioc_summaries`, MISP `search_misp_attributes`, existing OpenCTI knowledge `list_opencti_observables`.
- ATT&CK references: MITRE ATT&CK `get_mitre_attack_object` / `search_mitre_attack`.
- Validation: `stix_bundle_validator` for the bundle when available.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to give [stakeholder team names] an enriched set of indicators that stays tied to its source and context, ready to import into a TIP.
</context>

<task>
From the [data], extract every indicator of compromise. For each one, keep the verbatim sentence it appeared in, the source name and URL, and any associated malware, ATT&CK technique, campaign, or threat actor. Enrich each indicator: if you have web or tool access (see <collection>), populate the enrichment inline; if not, mark the enrichment field "to enrich". Produce the expanded STIX 2.1 bundle described in the output format.
</task>

<output_format>
First, a STIX 2.1 bundle following this structure: indicator SDOs (pattern, pattern_type, pattern_version, valid_from, name, the verbatim context in description, indicator_types, kill_chain_phases, confidence, labels, external_references for source and ATT&CK, and a SOC-handling property-extension carrying recommended_action, priority, log_sources, environment_tags, detection_notes, and false_positive_notes). Declare the SOC-handling extension as an extension-definition SDO and key it in "extensions" by that object's "extension-definition--UUID" id (NOT by a bare name such as "x-org-soc-handling", which fails strict STIX 2.1 validation). Also include campaign, attack-pattern, course-of-action, identity, and relationship ("indicates", "uses", "mitigates") objects where the report supports them. Do NOT put "spec_version" on the bundle object itself; it belongs only on the individual SDOs. Leave sighting and internal-only fields as placeholders. Return the bundle in its own code block as valid JSON with no comments.

Then, a short Markdown table for validation:
| Indicator | Type | Enrichment (reputation, first seen, classification) | Context (verbatim) | Source | Associated malware/TTP/campaign/actor |
|-----------|------|------------------------------------------------------|--------------------|--------|---------------------------------------|

Finally, only when [write to OpenCTI] is true: a short "OpenCTI draft" line with the draft id and the number of observables and indicators created (see <opencti_write>).
</output_format>

<opencti_write>
Only when [write to OpenCTI] is true: after producing the bundle and table, create an OpenCTI draft with `create_opencti_draft`, then add each extracted indicator to it: `create_opencti_observable` for the observable, and `create_opencti_indicator` with `pattern_type` "stix" and the STIX pattern from the bundle, carrying the verbatim context as description and the confidence you assigned. Do not create indicators for entries marked "monitor-only" without saying so in the draft line. Report the draft id so an analyst can review and approve it. When [write to OpenCTI] is false or empty, never write to OpenCTI or any other platform.
</opencti_write>

<guidelines>
1. Extract only indicators present in [data]; do not infer or generate them.
2. Normalize defanged indicators (hxxp to http, [.] to a dot).
3. Use STIX 2.1 patterning per type: [file:hashes.'SHA-256' = '...'], [ipv4-addr:value = '...'], [domain-name:value = '...'], [url:value = '...'], [email-addr:value = '...'].
4. The context (indicator description) MUST be a verbatim quote from [data].
5. If enrichment data is not available to you, mark the field "to enrich" rather than guessing reputation or classification.
6. The bundle must parse as valid STIX 2.1 JSON with no inline comments, and must validate in strict mode (stix2 allow_custom=False). Run `stix_bundle_validator` on it when available and fix what it reports.
7. If an indicator is a legitimate system binary or service being abused rather than a malicious artifact, extract it but set "recommended_action": "monitor-only" in the SOC-handling extension and add a note in false_positive_notes flagging it as a legitimate binary.
8. If an indicator is a legitimate third-party service endpoint or domain being abused for C2 (such as OAuth endpoints, cloud storage, or SaaS platforms), extract it with "recommended_action": "monitor-only", set false_positive_notes to flag it as shared infrastructure where blocking would cause widespread disruption, and add a detection_notes value recommending behavioral detection over network blocking.
9. Ensure the full source name, report title, and URL are attached to each indicator, e.g., in the table, each row in the source column should contain the source name, report title, and report URL.
</guidelines>
