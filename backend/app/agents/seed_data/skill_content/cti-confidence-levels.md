# Confidence Levels

Every analytical judgment MUST carry a confidence level. Confidence reflects the quality and quantity of evidence supporting the judgment, NOT the probability that the event will occur (that is likelihood: see the **Likelihood Language** skill).

## Primary Scale: Named Bands

The 0-100 score uses the same scale as the MISP confidence taxonomy and the OpenCTI `confidence` field.

| Band | Score Range | Meaning |
|------|-----------|---------|
| **Very High** | 90-100 | Based on high-quality information from multiple independent sources. Analyst has no significant concerns about the validity of the sources. Well-corroborated assessment. |
| **High** | 75-89 | Based on high-quality information, or from multiple sources with minor inconsistencies. Key assumptions are well-supported. |
| **Moderate** | 50-74 | Based on credible information that is not sufficient to warrant higher confidence. Key assumptions are reasonable but not fully validated. Alternative interpretations exist. |
| **Low** | 25-49 | Based on limited or fragmentary information. Key assumptions have significant uncertainty. Multiple plausible alternative interpretations. |
| **Very Low** | 0-24 | Based on sparse, unreliable, or largely circumstantial information. Assessment is essentially speculative. |

## What Determines Confidence

Confidence is determined by three factors.

### 1. Quality of Sources
- How reliable are the sources? (Cross-reference with the Admiralty Scale: see the **Source Assessment (Admiralty Scale)** skill.)
- Are sources independent or derivative?
- Is there potential for deception or disinformation?

### 2. Quantity and Corroboration
- How many independent sources support the assessment?
- Do sources confirm each other or contradict?
- Is the evidence diverse (technical + human + open source)?

### 3. Analytical Logic
- How strong is the analytical reasoning?
- Have key assumptions been tested?
- Have alternative hypotheses been considered and evaluated?

Integrations that were not connected or returned nothing reduce the evidence base: say which sources were skipped, and do not count a skipped source as corroboration or as contradiction.

## How to Express Confidence

### In text

> "We assess with **high confidence** that APT28 is responsible for this campaign, based on infrastructure overlaps with three previously attributed operations and consistent TTP alignment documented by two independent vendors."

### In a product header or metadata block

```yaml
confidence: high
confidence_score: 82
confidence_rationale: "Based on corroboration from CrowdStrike and Microsoft reporting, plus internal telemetry matching known APT28 C2 patterns."
```

### In tables/structured output

| Assessment | Confidence | Rationale |
|-----------|-----------|-----------|
| APT28 attribution | High (82) | 3 independent source corroboration + TTP match |

### In OpenCTI

OpenCTI objects carry a `confidence` field on the same 0-100 scale. When the user or the assignment asks to record a judgment in OpenCTI, set the score when creating the object, or change it with `update_opencti_field`, and put the rationale in a Note (`create_opencti_note`). The platform may cap what a user can set to that user's own confidence level. Never write to OpenCTI unless asked.

## Confidence vs Likelihood

These are DIFFERENT concepts:

- **Confidence**: how sure are we about our assessment? (Evidence quality.)
- **Likelihood**: how probable is a future event? (Predictive judgment.)

Example:

> "We assess with **high confidence** that Group X has the capability to target financial sector organisations. It is **likely** (50-75%) that they will conduct such operations in the next 12 months."

The confidence in the capability assessment is high (strong evidence). The likelihood of future targeting is a separate judgment.

## Calibrating Confidence

### Upgrade confidence when:
- A new independent source corroborates the assessment
- Technical evidence directly supports the judgment
- Key assumptions are validated through testing

### Downgrade confidence when:
- A previously reliable source is found to have errors
- New information contradicts a key assumption
- Evidence turns out to be derivative (multiple reports trace back to one source)

## Common Mistakes

- Conflating confidence with likelihood ("high confidence it will happen": this is two concepts mashed together)
- Not explaining WHY confidence is at a given level (always include the rationale)
- Defaulting to "moderate" to avoid commitment: if the evidence is strong, say so
- Changing confidence based on the desired outcome rather than the evidence
- Treating confidence as static: reassess when new evidence emerges
