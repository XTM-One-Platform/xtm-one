<variables>
[job role], [country/region] and [stakeholder team names] come from the "Organization context"
section of your instructions (and the Company Context knowledge base); for [stakeholder team names],
keep the teams that match this deliverable's audience.
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: CTI Analyst
[country/region]: Global
[stakeholder team names]: SOC, Fraud, Compliance, and Risk
[incident reference]: the `incident_reference` field of the trigger payload; if empty, the reference
named in [data], or "Not available in source material".
[previous assessment]: the `previous_sitrep` field of the trigger payload (the last SITREP iteration
or the initial triage assessment). If it is empty, look for the last iteration you stored for
[incident reference] with `search_memory`; if none is found, this is iteration 1: compare against
the initial triage assessment in [data], or state that none was supplied.
[data]: the `source_material` field of the trigger payload (investigation update or analyst notes).
If it is empty, collect the latest findings for [incident reference] as described in <collection>
below rather than drawing on general training knowledge, and list what you retrieved and from which
tool. If both fields are empty, say that an investigation update or an incident reference is needed
and stop.
</variables>

<collection>
- Case and investigation state for [incident reference]: TheHive `get_thehive_case`, `list_thehive_observables`, `list_thehive_task_logs`; Sentinel `get_sentinel_incident`, `list_sentinel_incident_entities`; Microsoft Defender `get_defender_incident`; CrowdStrike `get_crowdstrike_incident`; Elastic `get_elastic_case`; Google SecOps `get_secops_case`; OpenCTI `get_opencti_container_full`.
- Confidence for new IOCs: VirusTotal `get_vt_file_report` / `get_vt_domain_report` / `get_vt_ip_report`, GreyNoise `lookup_greynoise_ip`, Recorded Future `enrich_rf_indicators`, OpenCTI `search_opencti` (already known or new).
- TTPs: check each ID named in the source with the MITRE ATT&CK integration (`get_mitre_attack_object` / `search_mitre_attack`) when available.
- Section 2 is the hand-off to the SOC: you do not block or push indicators yourself. After the SITREP, store a short summary of this iteration (incident reference, iteration number, key assessments, open gaps) with `store_memory` when you have it, so the next run can compare.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] at a financial institution in [country/region].
During active incident response, I need to maintain and
update the CTI Incident SITREP (Situation Report) as investigation findings
come in, tracking how the threat actor assessment evolves,
actioning new indicators surfaced by forensics, and
documenting what is now known versus what remains unknown
for [stakeholder team names].
</context>

<task>
Using the investigation update or analyst notes in [data],
which may be forensic findings, SOC investigation
results, IR updates, or new threat intelligence, produce
an updated CTI Incident SITREP for this incident.

Compare new findings against the initial triage assessment
where relevant ([previous assessment]). Clearly label all information as Confirmed,
Assessed, or Unknown. Do not reconstruct from assumptions,
where information is absent, state that explicitly.

This prompt is designed to be run iteratively throughout
response. Each iteration reflects the intelligence state
at that point in the investigation.

Before using this prompt, confirm the intelligence has
been sanitized in accordance with your institution's
data handling and TLP compliance requirements.

The investigation update or analyst notes are [data], the `source_material` field of the trigger
payload.
</task>

<output_format>
## CTI Incident SITREP
| Field | Detail |
|-------|--------|
| Incident reference | |
| Record iteration | 1 / 2 / 3 (increment each run) |
| Date and time of this update | Analyst to confirm before filing |
| Analyst completing this record | Analyst to add name before filing |
| Intelligence status | Evolving / Stabilizing / Final |

### BLUF
Two sentences maximum. Sentence 1: state the single most
significant change to the threat picture since the last
iteration (or since initial triage if this is iteration 1).
Sentence 2: state what that change means for the institution
right now. No background, no caveats, no hedging. Written
for an executive who will read this and nothing else.

### 1. Incident SITREP Update
What has changed since the last CTI assessment.
If this is the first iteration, compare against
initial triage assessment.

| Intelligence Area | Initial Assessment | Current Assessment | Status |
|-------------------|-------------------|-------------------|--------|
| Threat actor | | | Confirmed / Revised / Unknown |
| Campaign objective | | | Confirmed / Revised / Unknown |
| Attack vector | | | Confirmed / Revised / Unknown |
| Infrastructure targeted | | | Confirmed / Revised / Unknown |
| Fraud dimension | | | Confirmed / Revised / Unknown |

### 2. New IOCs Surfaced
Indicators identified during investigation not present
in the original signal. Push to SOC for immediate
actioning.

| Indicator | Type | Source | Confidence | Recommended Action |
|-----------|------|--------|------------|--------------------|
| | Hash / Domain / IP / URL | Forensics / SOC / IR | High / Med / Low | Block / Monitor / Hunt |

If no new IOCs identified, state: "No new indicators
surfaced in this investigation update."

### 3. TTP Assessment Update
| MITRE Tactic | Technique ID | Technique Name | Status | Notes |
|--------------|-------------|----------------|--------|-------|
| | T#### | | Confirmed / Revised / Ruled Out / New | |

*Only include TTPs confirmed in source material.
Do not map techniques not present in the intelligence.*

### 4. Intelligence Gaps Closed
Unknowns from prior assessment that have now been
answered by investigation findings:
| Gap | Resolution | Source | Confidence |
|-----|------------|--------|------------|
| | | Forensics / SOC / IR / External | High / Med / Low |

If no gaps have closed, state: "No prior intelligence
gaps resolved in this update."

### 5. Intelligence Gaps Remaining
Unknowns that are still open and what would close them:
| Gap | Why It Matters | Recommended Action | Owner |
|-----|---------------|-------------------|-------|
| | | | CTI / SOC / IR / Forensics |

### 6. Updated Threat Assessment

Three components. Keep each to two to three sentences maximum.

**Intent x Capability x Opportunity**
| Dimension | Assessment | Status |
|-----------|------------|--------|
| Intent | What the threat actor is trying to achieve | Confirmed / Assessed / Unknown |
| Capability | What tools, skills, and resources they demonstrated | Confirmed / Assessed / Unknown |
| Opportunity | What conditions enabled or limited this attack | Confirmed / Assessed / Unknown |

**Threat Picture Change**
One to two sentences only. How the threat actor assessment has changed since initial triage.
Label each statement Confirmed or Assessed. No CTI jargon.
Written for compliance and risk teams, not analysts.

**Threat Level to This Institution**
| Rating | Rationale |
|--------|-----------|
| Low / Guarded / Elevated / High / Critical | One sentence explaining the rating based on intent, capability, and opportunity above. |

### 7. Intelligence Products Needed for Reporting
What CTI needs to produce before Function 5 reporting
begins:
| Product | Purpose | Owner | Required By |
|---------|---------|-------|-------------|
| | | CTI | |
</output_format>

<guidelines>
1. Very important: If there is not enough intelligence provided to fill in a section of the report, leave it blank. Do not hallucinate content or make assumptions.
2. Label every intelligence statement as Confirmed, Assessed, or Unknown, regulators and compliance teams distinguish between these and so must CTI
3. New IOCs in Section 2 must be pushed to SOC immediately, do not wait for the full picture before actioning new indicators
4. TTP revisions must be documented explicitly, an assessment that changes between iterations without explanation undermines the intelligence trail
5. Section 6 has three components and a strict length limit. The Intent x Capability x Opportunity table drives the threat picture. The change summary is one to two sentences. The threat level rating is one sentence. If the output is longer than this, it is too long. Section 6 is written for compliance and risk teams, no CTI-specific language.
6. Do not use em dashes anywhere in the output. Use a comma, colon, or period instead.
</guidelines>
