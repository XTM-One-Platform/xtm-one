# Contract: Agent Card (external, anonymous)

**Spec**: FR-010..FR-018, FR-042 | **Plan**: [../plan.md](../plan.md)

## Endpoint

```
GET /a2a/{card_token}/.well-known/agent-card.json
```

Anonymous — no credential (FR-011). `{card_token}` is opaque and per-exposure (FR-012, FR-042).

> **Routing prerequisite**: `"a2a"` must be added to `_API_NAMESPACES` in `app/main.py`, or an unknown token falls through to the SPA catch-all and returns HTTP 200 with `index.html` instead of a JSON 404 (research R2). This is the single most easily-missed defect in the feature.

## Version negotiation

| Request | Card rendered as |
| --- | --- |
| `A2A-Version: 1.0` | v1.0 |
| `A2A-Version: 0.3` | v0.3 |
| header absent | **v1.0** (FR-027 — both platforms register without declaring a version, and parse as v1.0) |
| unrecognised value | `400` naming supported versions — decided before any agent lookup, so it discloses nothing |

## Responses

| Condition | Status | Body |
| --- | --- | --- |
| Live exposure, encrypted connection | `200` | Agent Card |
| Unknown token, revoked exposure, agent disabled, feature flag off | `404` | uniform not-found (FR-026) — all four indistinguishable |
| Connection not confirmably encrypted | `403` | refused, never redirected (FR-017) |

## Card projection

Built by explicit field assignment — **never a model dump** of an internal object, so an added internal field can never leak into a public document.

| Card field | Source |
| --- | --- |
| `name`, `description` | `Agent.name`, `Agent.description` |
| `skills` | the agent's configured built-in tools, in full (FR-034) |
| input content types | text and structured data (FR-024) |
| output content types | text and file always; structured **only** if the agent is configured for it (FR-015) |
| `securitySchemes` | OAuth 2.0 client credentials, pointing at the existing AS metadata |
| the security **requirement** | version-shaped: `security` under v0.3, `securityRequirements` under v1.0 |
| `interfaces` | one JSON-RPC binding at the task endpoint below |
| `capabilities.streaming` | `false` (FR-016) |
| `capabilities.pushNotifications` | `false` (FR-016) |
| `signature` | **absent** this phase; the representation stays forward-compatible so adding it later needs no partner re-registration (FR-018) |

The security requirement is spelled differently per version and must be shaped
like every other divergent field. A v0.3 reader handed the v1.0 name
(`securityRequirements`) does not see an unknown field — it sees *no* security
requirement at all, i.e. it is told the agent needs no credential. Since both
target platforms configure themselves from the card, getting this wrong
registers every partner unauthenticated and 401s them on their first
submission.

`tenant` is omitted, and a partner-supplied value is ignored rather than mapped — mapping it to `is_company_managed` would create the divergent second tenancy concept the research warns against.
