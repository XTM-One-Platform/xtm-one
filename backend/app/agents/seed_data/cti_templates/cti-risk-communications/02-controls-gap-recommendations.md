<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base); for
[stakeholder team names], keep the teams that match this deliverable's audience.
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: security operations and engineering
[data]: the `source_material` field of the trigger payload (the exposed-products and gap list,
for example the output of a CMDB scoping step). If it is empty, build the list for the threat named
in the `threat_focus` field as described in <collection> below rather than drawing on general
training knowledge, label it "agent-built, analyst to confirm", and list what you retrieved and from
which tool. If both fields are empty, say that a gap list or a threat focus is needed and stop.
</variables>

<collection>
- In-scope techniques for the threat: OpenCTI `search_opencti`, `get_opencti_entity`, `list_opencti_relationships` (attack patterns used); MITRE ATT&CK `get_mitre_attack_object` / `get_mitre_attack_related` to check IDs and mitigations.
- Products we confirmed we run: the Company Context knowledge base (`search_knowledge`); Microsoft Defender `list_defender_software`, `list_defender_vulnerabilities`; CrowdStrike `list_crowdstrike_vulnerabilities`; OpenAEV `search_openaev_assets`.
- Controls already implemented: Microsoft Defender `list_defender_secure_score_controls`, `list_defender_security_recommendations`; OpenAEV `search_openaev_findings` (validation results).
- Countermeasures: D3FEND has no XTM One integration; verify technique names on d3fend.mitre.org with `web_fetch` when available, otherwise mark the mapping [ANALYST CONFIRMS].
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to give [stakeholder team names] prioritized recommendations to close the control gaps for products we confirmed we run, with our existing controls taken into account.
</context>

<task>
Using the exposed-products and gap list in [data], draft prioritized recommendations to close the gaps. For each gap, give the in-scope technique it addresses, the affected product, the recommended countermeasure, and a first-pass priority.
</task>

<output_format>
| Exposed product | In-scope technique | Gap | Recommended countermeasure (D3FEND, NIST CSF, or CIS) | First-pass priority | Owning team | Notes |
|-----------------|--------------------|-----|-------------------------------------------------------|---------------------|-------------|-------|
</output_format>

<guidelines>
1. Recommend only against gaps present in [data]; do not introduce controls for techniques not listed or for products not confirmed in scope.
2. Skip any countermeasure already marked as implemented.
3. First-pass priority and the owning team are suggestions for the analyst to confirm against business criticality and implementation effort. Mark effort estimates as [ANALYST CONFIRMS].
4. Use estimative language. Avoid absolute claims about effectiveness.
5. Return Markdown only.
</guidelines>
