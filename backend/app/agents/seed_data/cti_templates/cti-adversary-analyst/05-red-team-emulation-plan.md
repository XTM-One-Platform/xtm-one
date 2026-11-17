<variables>
[job role], [sector name], [country/region] and [stakeholder team names] come from the
"Organization context" section of your instructions (and the Company Context knowledge base).
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: Threat Intelligence Analyst
[sector name]: cross-industry
[country/region]: global
[stakeholder team names]: technical security stakeholders
[product/service]: the `product` field of the trigger payload; if empty, Threat intelligence report.
[data]: the `source_material` field of the trigger payload. If it is empty, collect source
material as described in <collection> below rather than drawing on general training knowledge,
and list what you retrieved and from which tool.
</variables>

<collection>
- Recent tradecraft reporting: OpenCTI `search_opencti`, `get_opencti_container_full`, `list_opencti_relationships`; Feedly `search_feedly_articles`, `get_feedly_article`; vendor reports via CrowdStrike `list_crowdstrike_intel_reports`, GTI `get_gti_threat_profile`, Recorded Future `search_rf_analyst_notes`.
- Techniques, mitigations (M-codes) and relationships: MITRE ATT&CK `get_mitre_attack_object`, `search_mitre_attack`, `get_mitre_attack_related`.
- Existing emulation content to reuse (read only): OpenAEV `search_openaev_payloads`, `search_openaev_attack_patterns`, `search_openaev_scenarios`.
- Known detections: Recorded Future `search_rf_detection_rules`; OpenCTI indicators with a sigma `pattern_type` via `list_opencti_entities`.
- Open web: `search_google_web` and `web_fetch` when available.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] in the [sector name] industry, in [country/region]. My goal is to provide [stakeholder team names] team(s) with [product/service].
</context>

<task>
Read the provided [data] and generate a structured Adversary Emulation Report to support Red Team operations. The goal is to simulate realistic threat actor behavior based on recent tradecraft observed in the wild.
</task>

<output_format>
## 1. Summary of Threat Activity
1. Threat Actor (if known)
2. Campaign or Attack Name (if applicable)
3. Targeted Sectors/Industries
4. Reported Objective (e.g., data exfiltration, ransomware deployment, espionage)
5. MITRE ATT&CK Tactics & Techniques
6. Tools, Malware, or Frameworks Used
7. Relevant CVEs (if exploitation was part of initial access or lateral movement)

## 2. Attack Procedures (Emulation Steps)
For each procedure, provide the following details:
1. Step Name (short title of the simulated attack phase)
2. Tactic (MITRE ATT&CK Tactic, e.g., Initial Access, Persistence)
3. Technique ID & Name (MITRE ATT&CK Technique, e.g., T1566.001 Spearphishing Attachment); check each ID with the MITRE ATT&CK integration (`get_mitre_attack_object` / `search_mitre_attack`) when available
4. Procedure Description (technical detail of how the technique was used, including commands, payloads, or LOLBins; highlight novel chaining techniques if observed)
5. Emulation Plan (exact steps to emulate this behavior with tools such as manual commands, Cobalt Strike, Brute Ratel, Mythic, Atomic Red Team, Caldera, or an existing OpenAEV payload found in <collection>; include preconditions and payload examples)
6. Environment Considerations (infrastructure or configurations required, e.g., phishing server, domain controller, PowerShell remoting)

## 3. Detection Opportunities
1. Log Sources & Event IDs
2. Telemetry Requirements
3. Known Sigma Rules or Detection Samples
4. Blind spots or evasions observed

## 4. Purple Team Collaboration Opportunities
1. Techniques that benefit from validation
2. Coordinated tests to measure fidelity, tune detections, or validate response
3. Suggested KPIs (dwell time, detection latency, alert-to-investigation timing)

## 5. Mitigations and Hardening
1. High-level mitigations aligned to MITRE M-codes (e.g., M1047, Audit PowerShell logging)
</output_format>

<guidelines>
1. Ensure emulation steps reflect observed tradecraft from the [data]
2. Prioritize fidelity and align with MITRE ATT&CK standards
3. Avoid vague descriptions; use real payloads, delivery methods, and sources
4. Include specific tool commands and configuration details
5. Focus on actionable procedures that can be executed
6. Include citations to source material
7. Fill sections with details from the provided data ONLY. If information for any part of the report is not available in the source material, explicitly state 'Not available in provided reports' rather than filling gaps with generic knowledge
</guidelines>
