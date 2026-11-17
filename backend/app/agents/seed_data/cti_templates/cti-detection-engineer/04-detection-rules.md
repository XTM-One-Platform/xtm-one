<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: security operations center
[rule format]: the `rule_format` field of the trigger payload; if empty, Sigma.
[test in SIEM]: the `test_in_siem` field of the trigger payload; if empty, false.
[write to OpenCTI]: the `write_to_opencti` field of the trigger payload; if empty, false.
[data]: the `source_material` field of the trigger payload (the validated TTPs and IOCs). If it
is empty, collect source material as described in <collection> below rather than drawing on
general training knowledge, and list what you retrieved and from which tool in a short note after
the rules.
</variables>

<collection>
- Validated TTPs and IOCs: OpenCTI `search_opencti`, `get_opencti_container_full`, `list_opencti_relationships`, `list_opencti_observables`; Feedly `search_feedly_articles`, `get_feedly_article`, `get_feedly_stream_iocs`.
- Technique tags: MITRE ATT&CK `get_mitre_attack_object` / `search_mitre_attack`.
- Existing coverage, read only: Sentinel `list_sentinel_analytics_rules`, Splunk `list_splunk_saved_searches`, Elastic `list_elastic_rules`, Google SecOps `list_secops_rules`, Recorded Future `search_rf_detection_rules`.
- Query testing, only when [test in SIEM] is true (see <siem_testing>).
- Open web: `search_google_web` and `web_fetch` when available.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<format_adapter>
[rule format] is one of: Sigma, Microsoft Sentinel KQL, Splunk SPL, Elastic EQL, Google SecOps YARA-L, CrowdStrike Falcon LogScale, YARA.
- Sigma: follow the prompt below exactly as written.
- Any other value: adapt the Sigma detection-rule prompt below to produce [rule format] rules instead, keeping the same source-grounding and experimental-status guidelines, and note the matching validator to run before deployment. To retarget: keep the prompt's structure and source-grounding, swap the Sigma YAML fields for the target format's syntax (carry title, id, status experimental, description, references, author, date, ATT&CK tags, data source, false positives and level as the fields, metadata or comments the format supports), and swap the validator for that format's linter.
</format_adapter>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to give the [stakeholder team names] [rule format] rules they can validate and deploy.
</context>

<task>
Using the validated TTPs and IOCs in [data], draft Sigma detection rules, one per detectable behavior or indicator set, following the SigmaHQ specification (or, when [rule format] is not Sigma, [rule format] rules retargeted per <format_adapter>).
</task>

<output_format>
For each rule, a Sigma YAML block with these fields in order:
- title: short, no "Detects" prefix
- id: a randomly generated UUIDv4
- status: experimental
- description: why the rule exists and when it triggers, without a "Detects" or "This rule will" prefix
- references: the source report URL or citation
- author: [job role]
- date: today's date in YYYY-MM-DD
- tags: the relevant attack.TACTIC and attack.TECHNIQUE_ID values
- logsource: category, product, and service as applicable
- detection: named selection group(s) and a condition
- falsepositives: known benign scenarios
- level: informational, low, medium, high, or critical
(When [rule format] is not Sigma, one block per rule in that format's syntax carrying the same information, then one line naming the validator to run before deployment.)
</output_format>

<siem_testing>
Only when [test in SIEM] is true and [rule format] is Microsoft Sentinel KQL, Splunk SPL, Elastic EQL or CrowdStrike Falcon LogScale: run each rule's query with `run_sentinel_kql_query`, `run_splunk_search`, `run_elastic_eql_query` or `search_crowdstrike_ngsiem` respectively, over a short lookback (24 hours to 7 days). After the rules, report per rule whether it parsed, the hit count and any error, and fix the query where it failed to parse; a query that ran is syntax-checked only, not validated. Never save, enable or deploy a rule. Otherwise do not run any query.
</siem_testing>

<opencti_write>
Only when [write to OpenCTI] is true: after drafting the rules, create an OpenCTI draft with `create_opencti_draft`, then one `create_opencti_indicator` per rule with the raw rule text as `pattern`, `pattern_type` matching [rule format] (sigma, kql, spl, eql, yara, or the platform's own vocabulary value for YARA-L or LogScale; if none exists, skip the rule and say so), `x_opencti_main_observable_type` set to the observable type the rule targets, `x_opencti_detection` true, and the rule description and ATT&CK reference. Report the draft id after the rules so an analyst can review and approve it. When [write to OpenCTI] is false or empty, never write to OpenCTI or any other platform.
</opencti_write>

<guidelines>
1. Base every rule on [data]; do not invent indicators or behaviors.
2. Prefer field-based detection over keyword matching where the report supports it, since field-based logic performs better in most SIEMs and static IOCs age quickly.
3. Keep the title short and avoid "Detects when ..."; write the description without "Detects" or "This rule will".
4. Tag each rule with the mapped ATT&CK tactic and technique IDs (for example attack.initial_access and attack.t1566); check each ID with the MITRE ATT&CK integration (`get_mitre_attack_object` / `search_mitre_attack`) when available.
5. Set status to experimental; every rule must be validated and tuned before deployment.
6. List realistic false positives, and note the log source and fields each rule depends on.
7. Return the rules only (followed, where they apply, by the validator line, the collection note, the <siem_testing> results and the OpenCTI draft id).
</guidelines>
