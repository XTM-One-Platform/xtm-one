# CTI Stakeholder Management

Intelligence that doesn't reach the right person in the right format might as well not exist. This skill ensures every product is tailored to its audience: who the stakeholders are, what each one needs, how to keep a stakeholder register, how to tailor a product, and which channel each TLP level allows.

## Stakeholder Identification

### Common CTI Stakeholders

| Stakeholder | Needs | Format | Cadence |
|-------------|-------|--------|---------|
| **CISO** | Strategic risk, business impact, investment justification | Executive summary, risk scores, trend analysis | Monthly + ad-hoc |
| **SOC Manager** | Operational context, detection priorities, hunt leads | Operational briefs, IOC packages, hunt playbooks | Weekly + real-time |
| **SOC Analysts** | Tactical indicators, detection rules, response guidance | IOCs, SIGMA/YARA rules, enriched alerts | Daily + real-time |
| **IR Team** | Campaign context, TTP detail, forensic indicators | Detailed campaign reports, investigation leads | Per-incident |
| **Security Architecture** | Threat landscape, emerging attack vectors, control gaps | Threat assessments, ATT&CK mapping, control recommendations | Quarterly |
| **Risk Management** | Threat trends, probability assessments, sector comparisons | Risk assessments, likelihood language, quantified impact | Quarterly |
| **Board / Executive** | High-level risk posture, material incidents, peer benchmarks | 1-page dashboard, non-technical language, visual aids | Quarterly + material events |
| **Legal / Compliance** | Regulatory threat intelligence, breach notification triggers | Compliance-focused briefs, attribution (when needed) | Ad-hoc |
| **IT Operations** | Vulnerability intelligence, patch priorities, asset exposure | Vulnerability assessments, prioritised patch lists | Weekly |

## Stakeholder Register

### Template

```markdown
| Stakeholder | Role | PIRs | Products | Format Preference | Cadence | Feedback Method |
|-------------|------|------|----------|-------------------|---------|-----------------|
| [Name/Title] | [Role] | PIR-001, PIR-003 | Threat assessments, weekly brief | Executive summary, max 1 page | Monthly | Email response |
```

Record stakeholders by role or title. Do not put personal contact details in the register unless the user provides them for that purpose.

### Where the register lives in XTM One

- **Agent memory** (default): keep the register as a memory entry with `store_memory` and read it back with `search_memory` ("stakeholder register"). Update the row, not a new copy, when a preference changes.
- **Knowledge base**: when the team keeps CTI reference documents in a knowledge base, the register can be stored there with `store_knowledge` and found with `search_knowledge`, so every CTI agent on the team sees the same register.
- **OpenCTI**: stakeholders map naturally to the PIRs they own (see the **PIR Management** skill: the stakeholder is written in the PIR description). Use `manage_opencti_pir` action `list` to see which PIRs each stakeholder owns.

When no register exists yet, build one from the user's answers ("Who receives CTI today? In what format? How often?") and offer to store it.

## Tailoring Intelligence Products

### For Executives (CISO, Board)
- **Lead with business impact**, not technical detail
- Use risk language: "This threat could result in..." (then give the likelihood band)
- Include peer comparisons: "Organisations in our sector have seen..."
- Recommend actions in business terms: "Investment in X reduces risk by..."
- Maximum 1-2 pages
- Visual aids (charts, traffic lights, risk matrices): a structured report from `generate_report`, or a chart image from `generate_image` / `code_interpreter`

### For Security Operations (SOC, IR)
- **Lead with actionable indicators**
- Include detection rules and hunt queries (the **Sigma Rule Writing**, **YARA Rule Writing** and **KQL Query Writing** skills)
- Provide full TTP chain with ATT&CK mapping
- Technical detail is expected and valued
- Response playbook pointers
- Real-time delivery for active threats
- Machine-readable packages (CSV, STIX, MISP) per the **IOC Export** skill

### For Risk / Compliance
- **Lead with likelihood and impact assessments**
- Use standardised likelihood language (probability yardstick: Almost certain >95%, Highly likely 75-95%, Likely 50-75%, Realistic possibility 25-50%, Unlikely 15-25%, Highly unlikely 5-15%, Remote <5%)
- Map to frameworks (NIST CSF, ISO 27001)
- Quantify where possible
- Regulatory implications highlighted

## Dissemination Matrix

| TLP | Distribution Channel | Notes |
|-----|---------------------|-------|
| RED | Direct communication only (in-person, encrypted message) | Named recipients only |
| AMBER+STRICT | Internal secure channel (encrypted email, restricted SharePoint) | Organisation only |
| AMBER | Secure channel + need-to-know partners | May share with clients/partners |
| GREEN | Community channels (ISAC portal, closed mailing lists) | Not public |
| CLEAR | Any channel (blog, public advisories) | Unrestricted |

In XTM One, a product reaches stakeholders through the chat, a generated report, an OpenCTI Report (marked with the TLP, visible to the OpenCTI groups allowed to see it), or a channel bot message (Teams, Slack, Telegram...). Rules:
- Check the TLP against the channel before delivery. A team chat channel is never appropriate for TLP:RED, and only for AMBER+STRICT / AMBER when its membership is limited to the allowed audience.
- Send a message on an external channel only when the user or the assignment asks for it, and name the channel and audience in the output.
- For the full TLP rules, load the **TLP Guide** skill if it is attached.

## Feedback Collection

After every significant product delivery:
1. Was the intelligence useful? (Yes/Partially/No)
2. Was it timely? (Yes/Too late/Too early)
3. Was the format appropriate? (Yes/Too technical/Too high-level)
4. What should we cover next? (Free text)

Track feedback in the stakeholder register (memory or knowledge base entry). Feed it into PIR refinement (see the **Intelligence Feedback Loops** skill).
