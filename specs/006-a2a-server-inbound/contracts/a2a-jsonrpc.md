# Contract: A2A Task Surface (external, authenticated)

**Spec**: FR-019..FR-028, FR-040, FR-043 | **Plan**: [../plan.md](../plan.md)

## Endpoint

```
POST /a2a/{card_token}/jsonrpc
Authorization: Bearer <token from client_credentials grant>
```

JSON-RPC 2.0 only. gRPC and HTTP+JSON bindings are out of scope.

## Authentication

Tokens come from the existing authorization server's **new** client-credentials grant (research R3 — this grant does not exist today):

```
POST /mcp/oauth/token
grant_type=client_credentials&client_id=...&client_secret=...
```

- The client is bound to an `oauth_a2a` **integration**; it can reach only the agents that integration is bound to (FR-040). Credentials minted by the older per-exposure flow stay scoped to their single agent.
- Presenting it against any other agent returns the uniform not-found, not a `403` — a `403` would confirm the other agent exists.
- `fcp-` and `mcp-` platform keys are **rejected outright** and the rejection is audit-logged (FR-020).

The issued token is **confined to this surface**, and confined by properties of
the token rather than by deny-lists on the surfaces it must not reach. It carries
the boundary user's id as `sub`, so anything authenticating on `sub` alone would
otherwise treat it as that user's session:

| Claim | Closes | Because |
| --- | --- | --- |
| `type: a2a_client` | `/mcp/*` | the MCP resource server admits only `type: mcp_oauth` |
| `scope: a2a:task` | `/api/v1/*` | the platform session dependency refuses any token carrying `scope`, on presence alone |

This surface checks the kind **positively** — only `a2a_client` is accepted —
rather than excluding kinds it does not want. Every platform JWT is signed with
the same secret, so a token minted for another purpose must not be able to task
an agent here merely by carrying a `client_id`. Without the confinement a partner
credential reaches the exposure endpoints themselves and can expose further
agents and mint further credentials.

### Credentials — both carried by a bound integration

An agent is reachable exactly while it binds an A2A integration (see
`app/a2a/providers.py`); binding is the whole exposure opt-in, and there is no
separate switch. Two providers exist, and an agent may bind either or both —
each admits its own partner, and both resolve to the same task surface, rate
budgets and audit trail:

| Provider | Credential the partner presents | Verified against |
| --- | --- | --- |
| `oauth_a2a` (**OAuth A2A**) | A platform-minted `client_id` / `client_secret` exchanged at the card's token URL for a short-lived bearer | The registration attached to that integration, valid on every agent binding it — checked against the agent's **current** bindings, so unbinding stops it on the next request |
| `entra_a2a` (**Foundry A2A**) | The partner's own Microsoft Entra token | The three facts pinned in that integration's config, re-read and re-validated per request |

For `entra_a2a`, what is checked per request:

| Pinned in the bound integration | Verified against the token |
| --- | --- |
| tenant id | signature (JWKS fetched from the **stored** tenant, never from the token's `iss`) and issuer (both the v1.0 and v2.0 spelling of that tenant) |
| audience | `aud`, exactly; tenant-wide audiences (Graph, ARM) are refused when the integration is saved AND per request |
| client id | any of `azp` / `appid` / `oid` / `sub` — the calling identity, across token versions |

Verification is RS256-only (alg-confusion safe) and fail-closed: unreachable
JWKS, expired, wrong tenant, wrong audience or wrong caller all fold into the
same uniform refusal as any other bad credential; the specific cause is logged
server-side only. A deleted integration — or one edited into an invalid or
shared-audience state — admits nothing until fixed.

**Run-as identity is inherited, not chosen.** External work executes as the
agent's owner (for a company-managed agent, whoever seeded it), because
exposure follows a binding and there is no moment at which to demand a
choice. An agent with no owner is never exposed.

## Methods

Each operation has one method name per protocol version — the vocabularies are disjoint, and **the method name decides the response's dialect, outranking the `A2A-Version` header**. The reference SDK's v1.0 client sends no version header on any request (it derives its dialect from the card), so the method is the only reliable statement of which parser awaits the response; the header keeps its contract of refusing a declared-but-unsupported version, and still shapes the card route, which has no method name to read. Both names of an operation share one rate-limit budget — a second vocabulary is not a second allowance.

| Operation | v0.3 method | v1.0 method | Rate limit |
| --- | --- | --- | --- |
| send — create a task (continuation refused, see below) | `message/send` | `SendMessage` | 10/min per registration |
| get — read state and artifacts | `tasks/get` | `GetTask` | 1800/min per registration |
| cancel — stop the underlying run | `tasks/cancel` | `CancelTask` | 1800/min per registration |

Both versions accept the same inbound envelope (the message under `params.message` with a camelCase `messageId`; task ids under `params.id`), so only responses are version-shaped. The v1.0 responses are **proto-JSON** — the reference client parses them with `json_format.ParseDict` against the SDK's protos, which is strict about field names and enum spellings. Three consequences, each the same class of bug as v0.3's `kind` discriminator: the `SendMessage` result wraps the task in a `SendMessageResponse` oneof (`{"task": …}`) where v0.3's result *is* the task; parts use the flat proto fields (`text` / `data` / `url` + `filename`), not the draft spellings (`structuredData`, nested file objects); and states and roles use the proto enum names (below). `GetTask`/`CancelTask` return a bare `Task` in both versions. v1.0 methods this surface does not serve (`ListTasks`, `SubscribeToTask`, the push-notification family) get the same `UnsupportedOperationError` as unknown strings.

An additional per-IP limit binds BEFORE authentication (default 60/min), covering the anonymous card fetch and jsonrpc requests that fail auth — the per-registration limits above cannot bind a caller who never authenticates. Rate limiting is **forced fail-closed** on this surface: a Redis outage refuses rather than admits, diverging from the platform default of `"open"` (research R8). On the task surface a throttled request returns the JSON-RPC envelope with `RateLimitedError` (-32029) and a `Retry-After` header, never a bare HTTP detail body. Any other method returns `UnsupportedOperationError`.

### `message/send`

- **Blocking is per-version and follows each version's default.** v0.3 is non-blocking unless `configuration.blocking: true` (Foundry sends no flag and polls). v1.0 **blocks by default** — the polarity flipped to `configuration.returnImmediately`, and the reference SDK's client depends on it: it has no polling loop, makes exactly one `SendMessage` call, and renders whatever task state comes back as final. A blocking send holds the HTTP response until the task is terminal, capped by `A2A_BLOCKING_SEND_TIMEOUT_SECONDS` (default 120 s); on expiry the task returns in whatever state it reached — the same non-terminal task an immediate return would have carried, never an error. The wait applies to deduplicated repeats too.
- Accepts text and structured-data parts (FR-024).
- Deduplicates on the client-minted message id, scoped to the registration, 24 h window (FR-043, research R9). A repeat returns the **existing** task; no second run starts.
- Any `taskId` (continuation) is refused with `UnsupportedOperationError`. External runs execute through a path on which `INPUT_REQUIRED` is unreachable — the human-input machinery belongs to the assignment engine — so there is never a paused task to resume; refusing is honest where silently forking a new task was not.
- Refused with `QuotaExhaustedError` (-32030), audit-logged naming the client, when the run-as user's usage allowance is already exhausted at submission (FR-036). An exhaustion that races the run instead fails the task, with the cause stated in its status message.
- Rejected before a run starts — surfacing as `REJECTED` — when the agent cannot accept the content type, or structured output is requested from an agent not configured for it.

### `tasks/get`

Returns the task with state projected from its `AssignmentRun` (mapping in research R4). A `FAILED` task carries its stored failure reason as `status.message` (an agent-role `Message` with one text part), so quota exhaustion is machine-distinguishable from a crash (FR-036). Only the creating client may read it; anyone else gets the uniform not-found (FR-021).

### `tasks/cancel`

Sets the cross-replica cancel signal and, where the run is local, the in-process cancel event. Terminal tasks return `TaskNotCancelableError`.

## Task states

`SUBMITTED` → `WORKING` → { `COMPLETED` | `FAILED` | `CANCELED` }, plus `REJECTED` for pre-run refusal. `INPUT_REQUIRED` is defined in the state mapping but unreachable this phase. Continuation is refused here and served by `010-a2a-handover-compliance`, which reaches it through a context rather than through this state. `AUTH_REQUIRED` is unused this phase.

The spelling is per version, and the authority is what the reference SDK **parses** — never either specification's prose, and never the SDK's enum *types*, which agree across versions where the parsers do not. v0.3 spells states lowercase — `canceled` included, one L, per the SDK's `a2a.compat.v0_3.types.TaskState`. v1.0's JSON-RPC binding is proto-JSON, whose enum fields accept only the proto enum names: `TASK_STATE_COMPLETED`, and `ROLE_AGENT` for message roles. The platform's internal `AssignmentRun.status` keeps its own `cancelled` spelling; the mapping to the wire happens at the boundary, never in storage.

## Errors

Structured envelope with a machine-readable code, never free text (FR-025). Named errors used: `TaskNotFoundError`, `TaskNotCancelableError`, `ContentTypeNotSupportedError`, `VersionNotSupportedError`, `UnsupportedOperationError`, `RateLimitedError`, `QuotaExhaustedError`. (`PushNotificationNotSupportedError` is defined and unused: push configuration is not served by this feature, and `010-a2a-handover-compliance` serves it rather than refusing it.)

**Uniform not-found** covers all of: unknown task id, task owned by another client, unknown or revoked exposure, agent disabled, feature flag off (FR-026). Quota exhaustion returns a structured error and is audit-logged naming the responsible client (FR-036).
