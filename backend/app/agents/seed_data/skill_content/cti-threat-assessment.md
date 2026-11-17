# Threat Assessment

A threat assessment evaluates the threat posed by a specific actor or scenario to a specific target. It combines Intent, Capability, and Opportunity into an overall Threat Level.

## Formula

**Threat = Intent + Capability + Opportunity**

All three must be present for a credible threat. A highly capable actor with no intent poses minimal threat. A motivated actor with no capability poses minimal threat.

## Assessment Components

### Intent: What does the adversary want?

| Rating | Criteria |
|--------|---------|
| **Demonstrated** | Active targeting observed, stated objectives, ongoing operations against similar targets |
| **Probable** | Historical targeting of similar organisations/sectors, geopolitical alignment, inferred from capability development |
| **Possible** | General capability exists, sector/geography falls within known interests, no direct indicators |
| **Unlikely** | No known interest in sector/geography, no historical targeting of similar targets |

**Evidence to assess intent:**
- Direct targeting of the organisation or sector
- Stated objectives (manifestos, claims, leaked documents)
- Historical targeting patterns
- Geopolitical/economic motivations
- Reconnaissance activity observed

### Capability: What can the adversary do?

| Rating | Criteria |
|--------|---------|
| **Advanced** | Zero-day exploitation, custom tooling, state-level resources, proven track record of complex operations |
| **Significant** | Sophisticated TTPs, modified tools, professional operations, ability to adapt |
| **Moderate** | Known exploits and commodity tools, some custom capability, competent operations |
| **Basic** | Script-kiddie level, publicly available tools only, limited operational security |

**Evidence to assess capability:**
- Known tools and malware sophistication
- Historical operations and their complexity
- Resources (financial, human, infrastructure)
- Ability to develop or acquire zero-days
- Operational security and counter-intelligence capability

### Opportunity: What attack surface exists?

| Rating | Criteria |
|--------|---------|
| **Significant** | Large internet-facing footprint, known unpatched vulnerabilities, supply chain exposure, limited security controls |
| **Moderate** | Some internet exposure, generally patched but gaps exist, reasonable security controls |
| **Limited** | Minimal attack surface, strong security controls, rapid patching, limited supply chain exposure |
| **Minimal** | Air-gapped or highly restricted, advanced security controls, comprehensive monitoring |

**Evidence to assess opportunity:**
- Internet-facing services and known vulnerabilities
- Supply chain relationships and third-party access
- Security control maturity (detection, response)
- Employee exposure (social media, conferences)
- Historical incidents and near-misses

## Gathering the Evidence

Use only the tools you actually have; when an integration is not connected, say which source was skipped instead of guessing its result, and lower the confidence of the component it would have informed.

| Component | Where to look |
|-----------|---------------|
| Intent | OpenCTI: the actor (`search_opencti`, `get_opencti_entity`) and its `targets` relationships to sectors, countries and organisations (`list_opencti_relationships`); recent reports (`list_opencti_entities`). Vendor actor profiles where connected: `get_gti_threat_profile`, `get_crowdstrike_actor`, `get_intel471_actor`, `search_rf_threat_actors`. Victim claims: `search_ransomlive_victims`. |
| Capability | OpenCTI: the actor's `uses` relationships to malware, tools and attack patterns; MITRE ATT&CK: `get_mitre_attack_object`, `get_mitre_attack_related`; zero-day and exploit history via `get_vulncheck_exploits`, `get_vulnintel_cve`. |
| Opportunity | The organisation's exposure: OpenAEV findings and assets (`search_openaev_findings`, `search_openaev_assets`) when connected; known exploited vulnerabilities affecting its products (`check_vulnintel_kev`, `get_vulncheck_kev`); defensive telemetry and recent incidents from the connected SIEM/EDR (for example `list_sentinel_incidents`, `list_defender_incidents`, `list_crowdstrike_incidents`). When the organisation's environment is unknown, say so: opportunity is then assessed generically for the sector and confidence is lowered. |

Rate every source with the Admiralty Scale (for example B2).

## Combining into Threat Level

| Level | Criteria |
|-------|---------|
| **CRITICAL** | Demonstrated intent + Advanced capability + Significant opportunity. Attack is imminent or ongoing. |
| **HIGH** | Strong intent indicators + Significant capability + Exploitable opportunity. Attack is highly likely. |
| **MODERATE** | Some intent indicators + Moderate capability + Some opportunity. Attack is a realistic possibility. |
| **LOW** | Limited intent indicators + Basic/Moderate capability + Limited opportunity. Attack is unlikely. |
| **NEGLIGIBLE** | No credible intent OR minimal capability OR no meaningful opportunity. |

## Output Template

```markdown
## Threat Assessment: [Subject]
**Date**: YYYY-MM-DD | **Confidence**: [Level] | **TLP**: [Level]

### Threat Level: [CRITICAL/HIGH/MODERATE/LOW/NEGLIGIBLE]

### Intent: [Rating]
[Assessment with evidence and confidence]

### Capability: [Rating]
[Assessment with evidence and confidence]

### Opportunity: [Rating]
[Assessment with evidence and confidence]

### Combined Assessment
[Synthesis of intent + capability + opportunity into overall threat level. Use likelihood language for forward-looking statements.]

### Key Assumptions
[Per the Key Assumptions Check technique]

### Recommended Mitigations
1. [Prioritised by impact on reducing threat level]

### Sources
[With Admiralty Scale ratings]
```

Rigor rules in compact form:
- **Confidence**: Very High (90-100), High (75-89), Moderate (50-74), Low (25-49), Very Low (0-24), always with a rationale.
- **Likelihood** for forward-looking statements: almost certain (>95%), highly likely (75-95%), likely (50-75%), realistic possibility (25-50%), unlikely (15-25%), highly unlikely (5-15%), remote (<5%).
- **TLP**: threat assessments default to TLP:AMBER, since they are often tailored to a specific stakeholder.
- **Key assumptions**: list the assumptions and flag any that are high importance and low confidence. Load the **Key Assumptions Check** skill if it is attached.

Deliver the assessment in the chat or run output; produce a document with `generate_report` when asked, and record it in OpenCTI (Report or Note) only when the user or the assignment asks for it.
