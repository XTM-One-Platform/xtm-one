# Data Model: Minimal A2A Client (Outbound)

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-08-31

Three new tables. Supersedes the 2026-08-27 version, which held every per-card fact on `a2a_integration_settings` — the shape FR-059 replaced, because a card describes exactly one agent and a partner exposing three publishes three cards. Under that shape, reaching three of a partner's agents meant three registrations, three copies of one credential and three places to revoke it. That version in turn superseded the 2026-08-26 one, whose `a2a_client_registrations` had no direction concept.

This is what shipped, in one Alembic revision (`a2a3outbound1`) chained on whatever main's head is at the time (re-pointed on each rebase; the A2A state it starts from is the one `a2a2intmodel1` left). Four revisions were developed on the branch and merged into it — most of what they did was to each other, and only the end state is worth reading.

---

## Existing tables, reused

### `integrations` — *is* the partner registration

A partner is a row here with `provider` in `{oauth_a2a, entra_a2a, google_a2a}`. Nothing is added to this table.

| Existing column | Carries |
|---|---|
| `provider` | Which mechanism. With `direction` (below) it determines the target, so FR-009 needs no separate platform field |
| `user_id`, `is_company_managed`, `shared_group_ids`, `shared_group_permissions` | Authority, unchanged (FR-002) |
| `api_key_enc` / `client_secret_enc` / `extra_config_enc` | Fernet-encrypted credentials for whichever direction is active (FR-004), cleared on a direction change (FR-058) |
| `token_url`, `access_token_enc`, `token_expires_at` | The short-lived token minted from them, refreshed by existing `http.py` helpers (FR-010) |
| `status`, `call_count`, `last_called_at` | Coarse status and volume, as for every integration. The finer **health verdict** lives on the side table |

### Read-only dependencies

- **`agents`** — `integration_bindings` (fixed) and `integration_template_bindings` (per-user) are the whole opt-in (FR-005), and what a binding confers now follows direction (FR-055).
- **`assignment_runs`** — the platform's universal run record; the chat path creates one too (`services/agent_chat.py`), so one reference covers both run paths FR-021 admits.
- **`a2a_exposures`** *(inbound, shipped)* — unchanged in shape. What changes is *when* a row is created: only for an **inbound** binding.

---

## New table: `a2a_integration_settings`

One row per A2A integration, `1:1` with `integrations`. Holds the partner relationship's **direction** and nothing else — every per-card fact lives on `a2a_client_targets` (FR-059).

| Field | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid PK | no | |
| `integration_id` | uuid FK → `integrations.id` `ON DELETE CASCADE`, **unique** | no | Cascade is FR-008: deleting the integration removes credentials and this row in one act |
| `direction` | varchar(8) | no | `inbound` \| `outbound`. Never both (FR-055). The one value the exposure hook reads |
| `created_at`, `updated_at` | timestamptz | no | |

**Indexes**: one UNIQUE index on `integration_id`, named as `create_all` names it. Not a unique constraint *plus* a plain index — that pair gave a migrated database two indexes on one column where a fresh install had one, and neither name matched the other's.

**No mode-dependent NULLs.** The 2026-08-27 version accepted ~14 columns that were NULL on every inbound row, and the plan's Complexity Tracking argued for them. FR-059 removed the question rather than answering it: the columns that were NULL for an inbound row are per-CARD, and now live on a table whose rows exist only for outbound registrations.

---

## New table: `a2a_client_targets`

One row per partner AGENT this registration can reach — one card each (FR-059). The registration owns the partner and its credential; a row here owns what is true of one agent.

| Field | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid PK | no | |
| `integration_id` | uuid FK → `integrations.id` `ON DELETE CASCADE` | no | The registration. Cascade for the same reason the settings row cascades (FR-008): no verdict outlives what it describes |
| `card_url` | text | **no** | Where the partner PUBLISHES this card. Operator-supplied or discovered (FR-014, FR-060). NOT NULL because this row *is* a card, and a nullable column would defeat the uniqueness below — PostgreSQL treats NULLs as distinct |
| `invocation_url` | text | yes | Where work is SENT: the endpoint the fetched card declares. Distinct from `card_url` and almost never equal to it. Written at verification and never re-derived at delegation time |
| `pinned_origin` | text | yes | Scheme + host + port verified against; every request is checked and a card declaring another origin is refused (FR-034) |
| `agreed_protocol_version` | varchar(16) | yes | Negotiated per agent at verification, declared on every request, never defaulted (FR-023) |
| `card_fingerprint` | varchar(64) | yes | Hash over this card's material fields — invocation endpoint, auth scheme, input and output content types, signature presence. What "materially changed" compares (FR-019) |
| `accepted_card_fingerprint` | varchar(64) | yes | What an authorised actor accepted. A mismatch suspends delegation to THIS agent, not to its siblings |
| `signature_state` | varchar(16) | yes | `verified` \| `unsigned`, NULL before the first verdict. Copied onto every delegation (FR-017) |
| `usability` | varchar(16), default `unusable` | no | `usable` \| `limited` \| `unusable`. The **health verdict** (FR-020), per agent. Fail-closed default: a row exists before verification, and one not yet verified is unusable rather than usable-by-omission |
| `unusable_reason` | varchar(48) | yes | From the closed set below. Never free text, never a credential |
| `last_verified_at` | timestamptz | yes | Drives the sweep's due-row scan and FR-050's staleness check |
| `last_verification_error` | text | yes | Operator-facing detail; no credential values |
| `consecutive_transient_failures` | integer, default 0 | no | Transient only; a durable failure never increments it (FR-053) |
| `circuit_open_until` | timestamptz | yes | Cooldown expiry, lengthening on repeat, cleared by a successful on-demand verification (FR-030). Per agent: one unreachable agent must not pause its siblings |
| `last_failure_kind` | varchar(16) | yes | `transient` \| `durable`. The single classification the breaker and the health verdict both read |
| `partner_discovery_path` | text | yes | Where this partner's card said it lists the agents our credential reaches, from the extension that card declares (FR-063). NULL means the card carried no declaration — which is why "no card read yet" is distinguished by `card_fingerprint` rather than by this column |
| `declared_name`, `declared_description` | text | yes | Shown before use (FR-018); cached from the card, never edited here |
| `declared_skills`, `declared_input_types`, `declared_output_types` | jsonb | yes | Same. The `*_types` are what FR-028's pre-flight reads |
| `created_at`, `updated_at` | timestamptz | no | |

**Constraints**: UNIQUE `(integration_id, card_url)`. Two rows for one card would verify the same agent twice and give a calling model two tools that do one thing, disagreeing the moment one verdict moved.

**Indexes**: `(integration_id)`; and `(last_verified_at NULLS FIRST)` for the sweep's due-row scan. The `NULLS FIRST` is load-bearing rather than decoration — the scan orders `last_verified_at ASC NULLS FIRST`, and PostgreSQL's default btree ordering is NULLS LAST, which cannot serve that ordering in either scan direction. Measured on 200k rows, a plain index is ignored entirely in favour of a sequential scan and a sort.

**Deliberately absent: the credential.** It belongs to the partner, and a per-target copy would be one secret stored N times with N chances to drift — the duplication this table exists to remove.

**`unusable_reason` closed set** — each value maps to a requirement, so no call site invents one:

`never_verified` (initial) · `card_unfetchable` (FR-011) · `card_malformed` (FR-011) · `transport_unverified` (FR-012) · `signature_invalid` (FR-015) · `no_common_protocol_version` (FR-051) · `no_common_transport` (FR-051) · `request_rejected_by_partner` (FR-026) · `credential_cannot_mint_token` (FR-010) · `credential_rejected_by_partner` (FR-010) · `destination_not_permitted` (FR-035) · `destination_blocked_by_deployment` (FR-054) · `origin_mismatch` (FR-034) · `card_change_unaccepted` (FR-019) · `signature_downgraded` (FR-052) · `protocol_version_downgraded` (FR-052)

### The rule that governs the shipped inbound path

**An A2A integration with no settings row is neither inbound nor outbound**: it exposes nothing and contributes no tool. A missing row degrades to a visible, recoverable "my agent is not exposed" rather than to an unintended exposure (Principle II). The migration's backfill and the create path both guarantee a row, so this is a backstop rather than a normal state.

### State transitions

```text
(created, direction fixed) ─inbound──► exposure created for each bound agent   [shipped path, now gated on direction]
                           └outbound─► (per partner agent, FR-059:)
                                        usable ──material change──► unusable[card_change_unaccepted]
                                        │  ▲                            │
                                        │  └────── accept change ───────┘
                                        ├──N consecutive transient──► usable, circuit open
                                        │            ◄── cooldown expiry, or successful on-demand verify ──┘
                                        └──durable failure──► unusable[<reason>] ──fix + verify──► usable

direction change ──► REFUSED while bound to any agent (FR-057); on success, previous direction's
                     credentials cleared (FR-058) and every target deleted — a target described an
                     agent this registration will no longer task
```

Three invariants the code must hold:

1. **A durable failure never touches `consecutive_transient_failures`, and a transient one never sets `usability = unusable`** — one classification, two consumers, no second opinion (FR-053).
2. **`usability` is never inferred at read time** — written when a verdict is produced, read everywhere else, so the health surface and the delegation pre-flight cannot disagree.
3. **Exposure follows `direction`, never provider membership alone** — the shipped hook's current test, left unchanged, would expose an agent bound to an outbound integration.

---

## New table: `a2a_delegations`

One row per delegation attempt — the protocol-shaped view of work sent outward, mirroring how `a2a_tasks` mirrors an inbound run.

| Field | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid PK | no | |
| `integration_id` | uuid FK → `integrations.id` `ON DELETE CASCADE` | no | The partner. Attribution keys on what was recorded here at submission, never inferred from an agent's current bindings (FR-042) |
| `target_id` | uuid FK → `a2a_client_targets.id` `ON DELETE SET NULL` | yes | WHICH of the partner's agents (FR-059). `SET NULL` rather than `CASCADE`: removing a partner agent must stop it contributing a tool without deleting the record of work already sent through it. Un-backfilled — rows predating targets carry no attribution nobody recorded |
| `agent_id` | uuid FK → `agents.id` | no | The calling agent (FR-040) |
| `user_id` | uuid FK → `users.id` | no | Whose boundary applied and whose usage was consumed (FR-037) |
| `run_ref` | uuid FK → `assignment_runs.id` `ON DELETE CASCADE` | yes | The calling run. NULL only between row creation and run resolution |
| `partner_task_id` | text | yes | As assigned by the partner; NULL if submission never succeeded |
| `protocol_version` | varchar(16) | no | The version this exchange used — recorded, not looked up later, since the registration's may since have renegotiated |
| `signature_state` | varchar(16) | no | Copied at submission. What makes FR-017's distinction legible on the run, not only on the registration |
| `state` | varchar(24) | no | Platform-side name via `a2a/protocol.py`'s existing map — never a partner string stored raw |
| `outcome` | varchar(32) | yes | `completed` \| `partner_failed` \| `timeout` \| `unreachable` \| `credential_rejected` \| `content_type_mismatch` \| `refused_preflight` \| `cancelled` — FR-029's five classes plus the two that never leave the platform |
| `failure_kind` | varchar(16) | yes | From the same classifier that feeds the breaker |
| `partner_reason` | text | yes | The partner's own words, preserved (FR-029) and enveloped before the calling model reads them; on a pre-flight refusal, this platform's machine reason for the gate that refused |
| `continuation_count` | integer, default 0 | no | How many `input-required` round trips. Not a limit — the run's budgets bound it (FR-048) — but what makes turn-farming visible rather than merely bounded |
| `submitted_at`, `terminal_at` | timestamptz | no / yes | |

**Indexes**: `(integration_id, submitted_at desc)` for a registration's activity list; `(run_ref)` for the continuation ownership check; `(target_id)`.

**Why `CASCADE` on `run_ref` when the inbound sibling uses `SET NULL`**: `A2ATask.run_id` is `SET NULL` because an inbound task is the *partner's* record and must outlive its run — a partner holds the id and must get a defined answer. A delegation is our record of work we sent, has no external holder, and FR-044 ties its lifetime to the run. `SET NULL` would also break FR-046, since a continuation is authorised by matching `run_ref`.

**Deliberately absent**: the message sent and the result returned. Payloads live where run content already lives; copying them creates a second store of partner-exchanged content with its own retention question.

---

## Validation rules

**On creating an A2A integration**
1. A settings row is created with `direction` set; there is no valid A2A integration without one.
2. Inbound: the shipped credential handling for that mechanism is unchanged.
3. Outbound: rules below.

**On changing direction**
1. Refused while bound to any agent, naming them (FR-057). `agents_binding()` already answers this.
2. On success, the previous direction's credentials are cleared (FR-058).

**At outbound registration (before a target is usable)** — per card, so one failing agent leaves its siblings usable
1. `card_url` is `https` and its host passes the destination guard — public, non-loopback, non-link-local, not cloud-metadata (FR-012, FR-035).
2. The card fetches, parses, and — if it carries a signature — verifies. An invalid signature is fatal and unoverridable (FR-011, FR-014, FR-015).
3. The card location serves what the mechanism expects; a mismatch fails registration (FR-009).
4. A protocol version is agreed; none in common leaves the row `unusable[no_common_protocol_version]` (FR-051).
5. `pinned_origin` is set from the verified location; a card whose invocation endpoint sits elsewhere is refused (FR-034).
6. Two registrations MAY share a `card_url` — a legitimate configuration with separate credentials or budgets against one partner (see spec Assumptions). Within ONE registration a card URL is unique, because two rows for one card would verify the same agent twice.
7. A card location may also arrive from the partner rather than the operator (FR-060), and is subject to every rule above without exception. Discovery decides what to LOOK at and never what to trust. It is attempted only where the partner's own card declared the discovery extension (FR-063), and a partner answering with no agents changes nothing.

**Before each delegation, in order** — every check below reads the TARGET the delegation is aimed at, except the switch, the binding and the direction, which are the registration's
1. Deployment switch on (FR-007); the agent binds this integration; its `direction` is `outbound` (FR-055); the target's `usability = usable`; its circuit closed (FR-030).
2. `last_verified_at` within the staleness threshold — otherwise re-verify, and **refuse if that cannot complete** (FR-050).
3. `card_fingerprint == accepted_card_fingerprint` (FR-019).
4. Required content types within `declared_input_types` / `declared_output_types` (FR-028).
5. Target origin equals `pinned_origin` (FR-034).

**On each partner response**
1. Corresponds to a task we submitted; shape and state recognised for `protocol_version`; within the ingest bound — otherwise refused whole (FR-026, FR-027).
2. Untrusted content, `prompt_guard` on the way back (FR-036, FR-047).
3. A continuation names a `partner_task_id` whose row carries this run's `run_ref` (FR-046).

**Never, on any path**
- A credential in `unusable_reason`, `last_verification_error`, a log line, a health message, or an API response (FR-004).
- Run history or platform credentials in an outbound payload (FR-032).
- A stale verdict used because re-verification failed (FR-050).
- An exposure created for an outbound integration (FR-055).
