# Intelligence Quality Control

Peer review checklist and quality standards for intelligence products. Apply it to any draft (report, assessment, flash, profile, IOC list, detection pack) before it is published, shared or sent to a stakeholder, whether the draft was written by a person or by an agent.

## Review Process

1. All products must be reviewed before they are published or shared
2. The review is performed by a reviewer separate from the author where possible (another analyst, or a reviewing agent different from the one that wrote the draft)
3. Products with CRITICAL issues are returned for revision
4. Products with 3+ MAJOR issues are returned for revision
5. MINOR issues are noted but do not block publication

## Peer Review Checklist

### A. Structural Integrity (Weight: 15%)
- [ ] Correct product template used
- [ ] Complete header metadata (title, date, TLP, confidence, author/source, scope)
- [ ] TLP marking present and correctly applied
- [ ] Logical section ordering
- [ ] Appropriate length for product type

### B. Analytical Rigor (Weight: 30%)
- [ ] Conclusions supported by evidence
- [ ] Key assumptions explicitly identified
- [ ] Alternative hypotheses considered (where applicable)
- [ ] Structured analytic techniques applied appropriately
- [ ] No logical leaps or unsupported assertions
- [ ] Temporal scope specified for all predictions
- [ ] Intelligence gaps acknowledged (including integrations that were not connected or returned nothing)

### C. Sourcing (Weight: 20%)
- [ ] All sources assessed with the Admiralty Scale
- [ ] Source ratings justified and reasonable
- [ ] Source diversity (not single-source unless noted)
- [ ] Derivative vs independent sources distinguished
- [ ] No unattributed claims

### D. Mandated Language (Weight: 20%)
- [ ] Confidence levels on all assessments (with rationale)
- [ ] Likelihood language on all predictions (with percentage ranges)
- [ ] No hedge-stacking
- [ ] No vague probabilistic language without a probability band
- [ ] Confidence and likelihood not conflated
- [ ] Facts clearly distinguished from assessments

### E. Technical Accuracy (Weight: 10%)
- [ ] MITRE ATT&CK mappings correct
- [ ] IOCs properly formatted and validated
- [ ] Technical claims verifiable
- [ ] No contradictions with known intelligence

### F. Writing Quality (Weight: 5%)
- [ ] BLUF in first paragraph
- [ ] Active voice
- [ ] Specific language (dates, numbers, names)
- [ ] No unnecessary jargon
- [ ] Clear, concise sentences

## Verifying with the Platform

Spot-check the claims that carry the product, with the tools you actually have. When an integration is not connected, say which check was skipped instead of guessing its result.

- **ATT&CK mappings**: confirm each technique ID and name with the MITRE ATT&CK integration (`get_mitre_attack_object`, `search_mitre_attack`); flag deprecated or revoked techniques and wrong sub-technique IDs.
- **IOC format**: use `code_interpreter` to check syntax (IPv4/IPv6, domains, URLs, MD5/SHA-1/SHA-256 lengths), duplicates and defanging consistency; check for benign infrastructure that should not be listed with `check_misp_warninglists` when MISP is connected.
- **STIX bundles**: validate with `stix_bundle_validator`.
- **Contradictions with known intelligence**: compare key claims (attribution, actor-to-malware links, victimology) with OpenCTI (`search_opencti`, `list_opencti_relationships`).
- **CVE claims**: check CVSS, EPSS and KEV status with the vulnerability intelligence tools (`get_vulnintel_cve`, `check_vulnintel_kev`).

## Scoring

Calculate a weighted score (0-10):
- Each section: 0 (none met) to 10 (all met)
- Weighted by the percentages above
- Final score = weighted average

| Score | Verdict |
|-------|---------|
| 8-10 | Approved: publish |
| 6-7 | Approved with minor revisions: can publish after fixes |
| 4-5 | Revision required: significant issues |
| 0-3 | Rejected: fundamental problems |

The verdict also follows the issue rules of the review process: any CRITICAL issue, or 3+ MAJOR issues, means revision required whatever the score.

## Issue Severity

- **CRITICAL**: factual error in a key finding, missing TLP on sensitive content, unsupported primary conclusion, potential harm if published as-is
- **MAJOR**: missing confidence level on an assessment, vague likelihood language on a prediction, missing source assessment on key evidence, logical flaw in reasoning, missing key section
- **MINOR**: grammar/formatting, style inconsistency, minor Admiralty rating disagreement, non-essential section could be improved

## Review Output

```markdown
## Quality Review: [Product Title]
**Reviewed**: YYYY-MM-DD | **Verdict**: [Approved / Approved with minor revisions / Revision required / Rejected] | **Score**: [x.x/10]

### Section Scores
| Section | Weight | Score (0-10) |
|---------|:---:|:---:|
| A. Structural Integrity | 15% | |
| B. Analytical Rigor | 30% | |
| C. Sourcing | 20% | |
| D. Mandated Language | 20% | |
| E. Technical Accuracy | 10% | |
| F. Writing Quality | 5% | |

### Issues
| # | Severity | Location | Issue | Required fix |
|---|----------|----------|-------|--------------|

### Checks Skipped
[Verifications that could not be run, and why]
```

For the full rules behind sections C and D, load the **Source Assessment (Admiralty Scale)** and **Confidence Levels** skills if they are attached.
