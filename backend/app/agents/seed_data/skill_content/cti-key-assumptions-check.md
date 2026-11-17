# Key Assumptions Check

Every analytical judgment rests on assumptions, often unstated. This technique surfaces them and evaluates whether the analysis holds if the assumptions are wrong.

## When to Use

- Before publishing a high-impact assessment
- When someone asks "what are we assuming?" or "are these assumptions still valid?"
- During any major assessment, before or after the core analysis (for example before an ACH, or as a check on a threat assessment)
- When a long-standing judgment is being reused and the situation may have changed

## Procedure

### Step 1: State the Analytical Line

Write the current assessment or conclusion in one clear sentence.

### Step 2: List All Assumptions

Brainstorm every assumption underlying the assessment. Include:
- Assumptions about threat actor intent or capability
- Assumptions about data completeness or accuracy
- Assumptions about the relevance of historical patterns
- Assumptions about the target environment
- Assumptions about timing or sequencing

### Step 3: Evaluate Each Assumption

| Assumption | Importance | Confidence | Status |
|-----------|:---:|:---:|:---:|
| [Assumption 1] | High | High | Validated |
| [Assumption 2] | High | Low | **FLAG** |
| [Assumption 3] | Medium | Medium | Monitor |
| [Assumption 4] | Low | High | Accepted |

**Importance**: how much does the conclusion depend on this assumption?
- **High**: if wrong, the conclusion changes significantly
- **Medium**: if wrong, the conclusion weakens but may still hold
- **Low**: if wrong, the conclusion is largely unaffected

**Confidence**: how sure are we this assumption is correct?
- **High**: strong evidence supports it
- **Medium**: some evidence, but not fully validated
- **Low**: little or no evidence; assumed by default

### Step 4: Flag Critical Assumptions

Any assumption that is **High Importance + Low Confidence** is critical. These are the assumptions most likely to invalidate your analysis.

### Step 5: Determine Impact

For each flagged assumption:
- What would the conclusion be if this assumption is wrong?
- Can this assumption be validated through additional collection?
- Should the confidence level of the overall assessment be lowered?

When validation is possible with the tools at hand, say which ones: for example checking that an actor is still active against OpenCTI (`search_opencti`, `list_opencti_relationships`), that infrastructure is still live with the enrichment integrations, or that a vulnerability is exploited in the wild with the vulnerability intelligence tools. Use only the tools you actually have; when an integration is not connected, say which source was skipped instead of guessing its result, and keep the assumption flagged.

## Output Template

```markdown
## Key Assumptions Check: [Assessment Title]

### Analytical Line
[The assessment being checked]

### Assumptions Matrix
| # | Assumption | Importance | Confidence | Status |
|---|-----------|:---:|:---:|:---:|
| 1 | ... | H/M/L | H/M/L | ... |

### Flagged Assumptions (High Importance + Low/Medium Confidence)
1. **[Assumption]**: If wrong, [impact on conclusion]. Collection gap: [what would validate this].

### Impact on Assessment
[Does the assumptions check change the confidence level? Should the conclusion be qualified?]

### Recommended Actions
- [Validate assumption X through Y collection]
- [Lower confidence from High to Moderate because of assumption Z]
```

If flagged assumptions must be re-checked later, and the user or assignment asks for it, record them in the agent's memory (`store_memory`) so a later run can find them with `search_memory`.
