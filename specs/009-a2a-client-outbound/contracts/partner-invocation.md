# Contract: Partner invocation (outbound wire)

**Feature**: [spec.md](../spec.md) | Serves FR-009, FR-045, FR-010–FR-016, FR-019, FR-022–FR-027, FR-034, FR-050–FR-052, FR-055

What this platform sends to a partner and what it will accept back. **Three** supported targets, one behaviour: nothing below differs per target except credential acquisition. Only an integration whose direction is `outbound` reaches any of this (FR-055).

All protocol shaping goes through `app/a2a/protocol.py`, which already owns v1.0 ↔ v0.3 conversion in both directions. The outbound path must never read its `DEFAULT_VERSION` constant — that default answers what to serve an inbound caller that declares nothing, and FR-023 forbids relying on any default when we are the client.

## Credential acquisition

| Mechanism (outbound) | Target | Stored | Exchanged for |
|---|---|---|---|
| `google_a2a` | Google Agent Platform | Service-account key material | A `cloud-platform`-scoped token, via a signed assertion |
| `entra_a2a` | Azure Foundry | Entra client credentials + tenant | A token from the pinned tenant's endpoint, carrying at least the role the agent-consumer surface requires |
| `oauth_a2a` | Any partner accepting OAuth client-credentials, **this platform's own inbound surface included** | Client id + secret + token endpoint | A bearer from the partner's token endpoint |

`oauth_a2a` and `entra_a2a` are the shipped inbound mechanisms switched to `outbound`. `google_a2a` was specified outbound-only, on the argument that a Google-side partner calling *us* authenticates through `oauth_a2a` inbound; that was withdrawn (spec Clarifications, 2026-08-31) and it now serves both directions — the OAuth path needs a shared secret and the Google one needs none.

Tokens are short-lived and re-minted just before expiry by the existing `integrations/http.py` helpers, with the reactive 401/403 retry as a backstop. Three failures are distinguishable, because FR-010 requires it:

| Failure | Kind | `unusable_reason` |
|---|---|---|
| Credentials cannot mint a token at all | durable | `credential_cannot_mint_token` |
| Transport to the partner fails | transient | — (breaker counts it) |
| Partner rejects a token we successfully minted | durable | `credential_rejected_by_partner` |

## Card retrieval

- **Location**: the target's `card_url`, exactly as supplied or discovered — one per partner agent (FR-059). Not derived from a base URL and not assumed to be at the well-known path; neither target serves it there (FR-014).
- **Authentication**: with the registration's own credentials **where the target requires them**. The two vendor targets do; the OAuth target may not — this platform serves its own cards anonymously at a well-known path, which is what lets a partner register from a URL alone. FR-014 was corrected to say exactly this (research R14).
- **Transport**: `https` only, over a connection confirmed encrypted. An unverifiable connection is refused, never downgraded to (FR-012).
- **Destination**: the host passes the destination guard before any request — public, non-loopback, non-link-local, not a cloud-metadata address. See research R6 for why this guard exists here rather than in a deployment-wide policy.

### Verification outcomes

| Card state | Outcome |
|---|---|
| Valid signature | `signature_state = verified` |
| Signature present, does not verify | **unusable, no override, ever** (FR-015) |
| No signature | `signature_state = unsigned`, registration usable — accepted on confirmed-encrypted transport plus the operator-supplied location (FR-016) |
| Unfetchable, malformed, or transport unconfirmable | unusable with the specific reason (FR-011, FR-012) |
| Invocation endpoint on another origin | refused; `pinned_origin` is authoritative (FR-034) |
| No protocol version in common | unusable, `no_common_protocol_version` (FR-051) |

Signature handling maps directly onto the SDK's own error split (`NoSignatureError` vs `InvalidSignaturesError`), so the two cases stay distinct in code as well as in prose.

### Material change

A re-verification computes `card_fingerprint` over the material fields only — invocation endpoint, authentication scheme, input content types, output content types, signature presence. A change against `accepted_card_fingerprint` suspends delegation until an actor with authority accepts it (FR-019).

Two changes are singled out as **downgrades**, suspending rather than silently renegotiating (FR-052), because a capability that was proved and then withdrawn is evidence rather than noise:

- a card that carried a signature and no longer does → `signature_downgraded`
- a partner that stops serving the agreed protocol version → `protocol_version_downgraded`

A partner that *gains* a newer supported version renegotiates upward without suspension, and the change is audit-logged.

### Freshness

- Periodic sweep at a stated interval (default hourly), claiming due rows with `FOR UPDATE SKIP LOCKED` so replicas divide the work.
- A verdict older than the staleness threshold (default 15 minutes) forces re-verification **before** the delegation is sent. If that re-verification cannot complete, the delegation is refused — the stale verdict is never a fallback (FR-050).

## Task submission and follow

Every request declares the agreed protocol version explicitly.

| Operation | Used for |
|---|---|
| Send message | Create the partner task from the message the agent composed — and only that (FR-032) |
| Send message on an existing task | A continuation after `input-required` (FR-031) |
| Get task | Poll to a terminal state or to `input-required` (FR-022) |
| Cancel task | Ceiling expiry, calling run cancelled or failed, or a run ending with a task still awaiting input (FR-024, FR-025) |

### Responses this platform refuses whole

Refused, never partially interpreted (FR-026, FR-027):

- does not correspond to a task we submitted
- shape does not match the agreed protocol version
- reports a task state not recognised for that version — never mapped to the nearest known state
- exceeds the ingest size bound (FR-027)
- declares content types the exchange cannot handle — refused **before** submitting where the card already says so (FR-028)

### Not supported

- **Streaming and push notifications.** This platform polls, symmetric with what the inbound surface declares unsupported.
- **Protocol bindings other than JSON-RPC.**
- **Following a redirect off `pinned_origin`**, in any circumstance.
