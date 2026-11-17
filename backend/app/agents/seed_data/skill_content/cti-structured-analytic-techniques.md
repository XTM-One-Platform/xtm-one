# Structured Analytic Techniques

Structured Analytic Techniques (SATs) are formal methods that externalise analytical thinking, making it transparent, challengeable, and less susceptible to cognitive biases. Choose the right technique based on the analytical challenge. This skill is the index: it tells you which technique to apply and which skill carries the full method.

## When to Use SATs

- When the stakes are high (wrong answer = significant consequence)
- When multiple plausible explanations exist
- When cognitive biases are likely (confirmation, anchoring, availability)
- When you need to communicate your reasoning transparently
- When building on analysis from multiple analysts

## Technique Categories

### Diagnostic Techniques

**Purpose**: evaluate existing analysis for quality and bias.

| Technique | When to Use | Skill |
|-----------|-------------|-------|
| **Key Assumptions Check** | Before or after any major assessment: surface and challenge unstated assumptions | **Key Assumptions Check** |
| **Quality of Information Check** | When evidence quality is uncertain: assess source reliability and information gaps | **Source Assessment (Admiralty Scale)** |
| **Indicators Validation** | When monitoring a developing situation: define observable markers of change | Part of **Horizon Scanning** |

### Contrarian Techniques

**Purpose**: challenge prevailing judgments and expose blind spots.

| Technique | When to Use | Skill |
|-----------|-------------|-------|
| **Analysis of Competing Hypotheses** | Multiple plausible explanations: systematically evaluate each against evidence | **Analysis of Competing Hypotheses** |
| **Devil's Advocate / Red Team** | When consensus is strong: deliberately argue the opposing position | **Red Team Analysis** |
| **What-If Analysis** | Explore low-probability but high-impact scenarios | Part of **Horizon Scanning** |
| **High-Impact / Low-Probability** | Ensure unlikely but catastrophic scenarios are considered | Combine **Threat Assessment** + **Horizon Scanning** |

### Imaginative Techniques

**Purpose**: generate new ideas, hypotheses, and indicators.

| Technique | When to Use | Skill |
|-----------|-------------|-------|
| **Brainstorming** | Generate hypotheses, IOC types to collect, detection approaches | No dedicated skill: apply freely |
| **Scenario Development** | Explore multiple futures for strategic planning | **Horizon Scanning** |
| **Indicators Generation** | Define early warning indicators for a scenario | **Horizon Scanning** |

## Decision Guide: Which Technique?

```
Are you evaluating WHO did something?
  -> Analysis of Competing Hypotheses

Are you assessing a THREAT?
  -> Threat Assessment

Are you challenging your OWN analysis?
  -> Key Assumptions Check

Are you challenging SOMEONE ELSE's analysis?
  -> Red Team Analysis (devil's advocate)

Are you looking FORWARD (emerging threats)?
  -> Horizon Scanning

Are you unsure if your evidence is RELIABLE?
  -> Source Assessment (Admiralty Scale)

Do you have MULTIPLE plausible explanations?
  -> Analysis of Competing Hypotheses
```

Load the named skill if it is attached to the agent. If it is not, apply the technique from the summary above and say that the full method was not available.

## Combining Techniques

For complex assessments, combine techniques:
1. **Key Assumptions Check** first (surface your assumptions)
2. **Analysis of Competing Hypotheses** for the core analysis (evaluate hypotheses)
3. **Red Team Analysis** to challenge the result (stress-test conclusions)
4. **Horizon Scanning** for forward-looking implications

A run can load a limited number of skills (at most 8 distinct skills), so load only the techniques the question needs. For most questions, one core technique plus one challenge technique is enough.
