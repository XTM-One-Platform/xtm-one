# OpenCTI NLQ Filter Schema Reference

Rules and patterns for constructing correct OpenCTI filter JSON. Follow \
these rules strictly when generating filters.

---

## Entity Type Mapping

Always include the correct `entity_type` based on context:

| User intent | entity_type values |
|---|---|
| Incident Responses | `["Case-Incident"]` |
| Vulnerabilities | `["Vulnerability"]` |
| IPv4 Addresses | `["IPv4-Addr"]` |
| Threat Actors | `["Threat-Actor-Group", "Threat-Actor-Individual", "Intrusion-Set"]` |
| Reports | `["Report"]` |
| Incidents | `["Incident"]` |

- If the input mentions a creator or assignee, use `creator_id` or \
`objectAssignee`.
- If filtering by CVSS score, always use `x_opencti_cvss_base_score`.
- **Do not forget** to include the entity_type alongside other filters. \
For example, "vulnerabilities with CVSS 4–6.9" requires both the CVSS \
filter AND `entity_type: ["Vulnerability"]`.

## Comparison Operators

| Operator | Meaning |
|---|---|
| `gt` | strictly greater than (> 6) |
| `gte` | greater than or equal to (>= 5) |
| `lt` | strictly less than (< 7) |
| `lte` | less than or equal to (<= 8) |
| `eq` | exact match (CVSS score of 9) |

- Be tolerant of spacing: treat `<=`, `< =`, and `≤` as `lte`; \
treat `>=`, `> =`, and `≥` as `gte`.
- Only the numeric part (e.g., `"4"`, `"3.5"`) goes in `values` — never \
include symbols like `>` or `<` in the value itself.

### CVSS Score Ranges

For ranges (e.g., "between 4 and 8"), always use **two distinct filters**:
- One for the lower bound (`gte` or `gt`)
- One for the upper bound (`lte` or `lt`)

Example — "between 4 and 8":
```json
{
  "mode": "and",
  "filters": [
    { "key": "x_opencti_cvss_base_score", "values": ["4"], "operator": "gt", "mode": "or" },
    { "key": "x_opencti_cvss_base_score", "values": ["8"], "operator": "lt", "mode": "or" }
  ]
}
```

## TLP Classification

Use `objectMarking` for TLP filters:
```json
{ "key": "objectMarking", "values": ["TLP:RED", "TLP:AMBER"] }
```
Always include the correct entity type alongside TLP filters.

## `regardingOf` Patterns

### Threats targeting a sector
```json
{
  "key": "regardingOf", "operator": "eq",
  "values": [
    { "key": "relationship_type", "values": ["targets"] },
    { "key": "id", "values": ["Healthcare"] }
  ]
}
```

### Information about a specific entity (e.g., APT28)
Use only `regardingOf` — **do not** add `entity_type` to allow flexibility:
```json
{ "key": "regardingOf", "operator": "eq", "values": ["APT28"] }
```

### Victims of a threat (e.g., emotet)
**Do NOT** specify `entity_type` to allow flexibility in victim types:
```json
{
  "key": "regardingOf", "operator": "eq",
  "values": [
    { "key": "relationship_type", "values": ["targets"] },
    { "key": "id", "values": ["emotet"] }
  ]
}
```

### Attack patterns used by a threat
Always include `relationship_type: "uses"`:
```json
{
  "key": "regardingOf", "operator": "eq",
  "values": [
    { "key": "relationship_type", "values": ["uses"] },
    { "key": "id", "values": ["APT28"] }
  ]
}
```
