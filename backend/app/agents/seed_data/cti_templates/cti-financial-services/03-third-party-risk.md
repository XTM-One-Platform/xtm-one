<variables>
[job role], [country/region] and [stakeholder team names] come from the "Organization context"
section of your instructions (and the Company Context knowledge base); for [stakeholder team names],
keep the teams that match this deliverable's audience.
Where one is missing or still marked TO FILL IN, use these defaults:
[job role]: CTI Analyst
[country/region]: Global
[stakeholder team names]: Risk, Operations, and Compliance Teams
Map service exposure and concentration risk against the Critical third parties, Payment
infrastructure and Identity and authentication systems lines of the Organization context (and the
Company Context knowledge base), and the regulatory flags against its Regulatory profile line;
where they are placeholders, mark the dependency "Not available in source material" and say so.
[provider]: the `provider_name` field of the trigger payload; if empty, the provider named in [data].
[data]: the `source_material` field of the trigger payload (incident report, vendor notification,
public URL or industry alert). If it is a URL, read it with `web_fetch`. If it is empty, collect
reporting on [provider] as described in <collection> below rather than drawing on general training
knowledge, and list what you retrieved and from which tool. If both fields are empty, say that an
incident report or a provider name is needed and stop.
</variables>

<collection>
- Incident reporting on [provider]: Feedly `search_feedly_articles`, `query_feedly_cyberattacks`; ransomware.live `search_ransomlive_victims`; OpenCTI `search_opencti`; Recorded Future `lookup_rf_company`; vendor status pages and advisories via `search_google_web` and `web_fetch` when available.
- The provider's exposed services, when the report names products or hosts: Shodan `search_shodan_hosts`, `get_shodan_domain_info` (read-only).
- Vulnerabilities named in the incident: `get_vulnintel_cve`, `check_vulnintel_kev`, `lookup_vulncheck_cve`.
- Which institutional services depend on [provider]: the Company Context knowledge base (`search_knowledge`); if it is silent, mark the dependency Unknown.
- Section 7 monitoring sources: give the URLs you actually retrieved or found, not guessed ones.
Use only the tools you have; name the sources you could not query. Never write to a platform unless the assignment says so.
</collection>

<context>
I'm a [job role] at a financial institution in [country/region].
I need to assess the downstream exposure of a third-party
provider incident, identifying which institutional services
are affected, what the customer-facing impact is, and whether
concentration risk across shared infrastructure amplifies
the threat beyond this single provider incident for
[stakeholder team names].
</context>

<task>
Using the provider incident report or alert in [data],
which may be a vendor notification, sector alert,
open-source intel report, or internal monitoring alert,
produce a third-party and ecosystem risk assessment.

Assess which institutional services are exposed, map
customer-facing impact, identify concentration risk across
shared providers, and flag any regulatory notification
implications triggered by the provider incident.

If the incident report is sparse or early-stage, document
what is known, flag what is unconfirmed, and recommend
monitoring actions while the incident develops.

State "Not available in source material" for any field
where information is absent, do not fill gaps with
assumptions.

Before using this prompt, confirm the intelligence has
been sanitized in accordance with your institution's
data handling and TLP compliance requirements.

The incident report, vendor notification, public URL or industry alert is [data], the
`source_material` field of the trigger payload.
</task>

<output_format>
## Third-Party and Ecosystem Risk Assessment

### 1. Incident Summary, Read First
| Field | Detail |
|-------|--------|
| Provider / vendor affected | |
| Incident type | Outage / Vulnerability / Breach / Ransomware / Other |
| Incident status | Confirmed / Developing / Unconfirmed |
| Source of this report | Vendor notification / Open-source report / Internal |
| Immediate action required | Yes / No, summarize in one line |

### 2. Institutional Service Exposure
| Service | Provider Dependency | Exposure Level | Current Status |
|---------|--------------------|--------------------|----------------|
| | Direct / Indirect | Confirmed / Probable / Possible | Affected / Monitoring / Unaffected |

### 3. Customer-Facing Impact
| Impact Area | Assessment | Confirmed / Estimated |
|-------------|------------|----------------------|
| Transaction processing | | |
| Account access | | |
| Payment authorization | | |
| Customer notifications required | | |

### 4. Concentration Risk
- Does this provider support multiple critical services
  at this institution? Yes / No / Unknown
- Are other institutions likely affected by the same
  incident? Yes / No / Unknown
- Is there a single-provider dependency that amplifies
  exposure beyond this incident? Yes / No / Unknown
- Concentration risk assessment:

### 5. Regulatory and Notification Flags
- Does this third-party incident trigger this institution's
  own regulatory notification obligations?
  Yes / No / Requires Review
- Basis for determination:
- Recommended compliance team action:

### 6. Monitoring and Response Actions
| Action | Owner | Priority | Timeline |
|--------|-------|----------|----------|
| | CTI / Risk / Operations / Compliance | Immediate / 24hr / 72hr | |

### 7. Intelligence Gaps
- What information from the provider would materially
  change this assessment?
- Recommended monitoring sources while incident develops: Add URLs for the sources.
</output_format>

<guidelines>
1. If the incident report is early-stage or sparse, do not wait for complete information, document what is known, flag gaps, and recommend interim monitoring actions.
2. Concentration risk must be assessed even when a single provider is named, shared infrastructure in BFSI means one incident can affect multiple institutions simultaneously.
3. Regulatory notification flags must consider the institution's own obligations, not just the provider's, a third-party breach may trigger your reporting requirements independently.
4. Customer-facing impact should be assessed separately from technical service exposure, operational teams need to know what customers will experience, not just which systems are affected.
5. Ensure response action timelines are based on industry best practices and standards, not AI generated/based on AI judgement.
6. Do not use em dashes anywhere in the output. Use a comma, colon, or period instead.
</guidelines>
