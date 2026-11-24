# Each person's own account on a shared integration

Status: **implemented**. The contract that ships is the "Shared
without the connected account" paragraph of `dev-docs/reference/backend-integrations-model-and-oauth.md`;
this page keeps the reasoning. Where the build departs from the review text below:

- **The flow does not change** (maintainer decision): sharing an OAuth
  integration still asks one thing - lend the connected account (the default)
  or `Don't share my connected account`. There is no separate mode choice,
  no `credential_mode` request field and no per-person API key: the opt-out
  *is* `per_user`, written by `apply_oauth_share_consent()`, and the fix is
  what it means - the owner's token is kept (it used to be removed, so the
  owner lost the connection), re-authorizing never lends it, and everyone
  else authorizes their own account. Migration `u5545ucred02` stores the
  opt-out of the integrations already shared that way.

- **The credential row is the token holder**, rather than a transient view of
  the integration: `IntegrationUserCredential` holds its tokens, expiry,
  status and account config, and reads every other `Integration` column from
  its integration through generated read-only properties. `_get_token`, the
  inline refresh and the sweep run on it unchanged; the steps keyed by the
  integration's identity (client secret and its vault record, relay route,
  client certificate) read `integration_of(holder)`, and the lock and back-off
  are keyed by `holder_ref(holder)` (`user-credential:<id>`; an integration's
  stays its bare id, so nothing changes for `shared` integrations).
- The resolver lives in `services/integration_user_credentials.py`, not in
  `integration_credentials.py` (which keeps the rule; `oauth_credentials_usable`
  now also refuses a `per_user` integration's stored API key to anyone but
  its holder, admins included).
- Decisions on the open questions: leaving `per_user` **deletes** the people's
  credentials; a company-managed `per_user` integration keeps an admin as its
  holder (the first to connect it); no owner lock against an admin; GitHub
  webhook routing keeps reading the holder's token; a run whose acting person
  has no credential runs without those tools and says so; a skill source
  refuses a `per_user` integration (shared and refreshed unattended).
- The provider-declared `account_config_keys` are kept (review): a person's
  credential reads the integration's `extra_config` without them
  (`configuration_extra()`), so a sign-in that learned nothing (`/me/accounts`
  failed, no Jira site) never falls back on the owner's page token or site;
  their account config holds those keys, and any other key their sign-in
  changed (`save_account_extra()`).
- **The holder does not connect first** (review): a member who connected
  their own account works while the integration's own status is not
  `connected` (the integrations `u5545ucred02` moved had lost their token).
  The gates on the acting person's paths read `connected_for_clause()` - the
  row is connected, or that person's own credential is - and a resolved
  holder's status is `holder_connected()`: KB sync sources, the pollers,
  agent accounts, the chat's session selection and handover prefetch, and
  the template resolution of chats, assignments and scheduled work.

A shared integration today lends **one** credential to everyone it is shared
with. This design separates what an integration *is* (its configuration) from
*whose account* it acts with (its credential), so a shared integration can let
each person connect their own account - an OAuth sign-in or an API key -
without anyone creating a second integration.

## The problem

An `Integration` row holds two different things:

| Configuration (shared by design) | Credential (personal by nature) |
|---|---|
| provider, base URL, `extra_config` settings, the OAuth app (`client_id_enc` / `client_secret_enc`, `authorize_url`, `token_url`, scopes), an imported OpenAPI spec and its selected tools, `tool_overrides`, relay route, sharing columns | `access_token_enc`, `refresh_token_enc`, `token_expires_at`, `api_key_enc`, and the account-derived part of `extra_config` (Jira `cloud_id` / `sites`, Facebook `pages` / `page_access_token`, LinkedIn `organizations`, GitHub installations) |

Sharing the row shares both. The personal-token rule
(`services/integration_credentials.py`) only decides whether the one OAuth
token is lent or withheld:

- **lent** (`oauth_share_acknowledged`): everyone acts as the owner;
- **withheld** (the `Don't share my connected account` switch): the token is
  removed, members get nothing, and the next authorization by the owner lends
  it again (`oauth_callbacks.acknowledge_share_on_authorize`). The opt-out is
  not a durable state: nothing records it.

The only way for each person to use their own account is the per-user
(template) binding: every person creates **their own integration** of the
same provider, and `shadow_invisible_bindings` falls back to it. That works
for a catalog provider whose OAuth app is a platform default (`OAuthDefault`),
and fails for everything that is configured per instance:

- an imported **OpenAPI** spec (SaaS Manager): the provider name `openapi`
  says nothing about which API, so it is excluded from per-user resolution
  (`PER_USER_UNRESOLVABLE_PROVIDERS`), and no member can use their own account
  at all;
- a **custom bundle**, a self-hosted instance (OpenCTI, Jira Data Center, an
  internal API) with its URL, relay route, TLS settings and selected tools:
  every member would re-create and re-maintain the same configuration to
  change one thing, the credential;
- **API-key** integrations: there is no personal mode at all; a shared key is
  everyone's key.

Reported case: an OpenAPI integration (SaaS Manager, OAuth 2) shared with a
group, its owner opted out of sharing the connected account, re-authorized,
and the new token was lent to the whole group again - while the intent was
"each member clicks Authorize and uses their own account".

## Goals

1. An integration's owner chooses, durably, whether the people it is shared
   with use **the owner's account** or **their own**.
2. In "their own" mode, a member connects their account **on the shared
   integration** (Authorize, or an API key), with no copy of its
   configuration.
3. Every provider, every auth type, the same mechanism: no provider, tool or
   OpenAPI special case in the generic layer (invariant 18).
4. Nothing that works today changes: `shared` is the default and behaves as
   on main.

Non-goals: per-user OAuth *apps* (the OAuth app stays the integration's
configuration); changing which identity a run acts as (see "Who acts as
whom"); MCP servers (their own token model, `MCPServer.access_token_enc`; the
same split may follow later, out of scope here).

## The model

### `Integration.credential_mode`

| Value | Who the integration's tools act as | Default |
|---|---|---|
| `shared` | The account stored on the integration, for everyone it is shared with - the rule of main, unchanged: an OAuth token is lent once its holder consented (`oauth_share_acknowledged`), an API key is shared by design. | yes |
| `per_user` | **Each acting person's own account**. The credential stored on the integration is its holder's (the owner, or `oauth_token_owner_id`) and is never lent; anyone else uses their own credential on the integration, or nothing. | |

`per_user` is a stored choice: authorizing again never changes it, which is
the durable opt-out the reported case lacked. `oauth_share_acknowledged` stays
the consent of `shared` mode and is always `False` in `per_user` mode.

A personal integration that is not shared with anyone has one user; the mode
is meaningful only once it is shared, and the UI offers it only then (the API
accepts it at any time, so a later share keeps the choice).

### `integration_user_credentials`

One row per (integration, user) - a member's own credential on a `per_user`
integration.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `integration_id` | UUID FK `integrations.id` `ON DELETE CASCADE` | |
| `user_id` | UUID FK `users.id` `ON DELETE CASCADE` | |
| `access_token_enc` | Fernet | OAuth |
| `refresh_token_enc` | Fernet | OAuth |
| `token_expires_at` | timestamptz | OAuth |
| `api_key_enc` | Fernet | API key / bot token |
| `account_config_enc` | Fernet JSON | the account-derived keys of `extra_config` for this account (see below) |
| `status` | text | `connected` / `expired` / `error`, the same vocabulary as `Integration.status` |
| `last_error` | text, nullable | the refresh or test verdict, shown to that user only |
| `created_at`, `updated_at` | timestamptz | |

Unique `(integration_id, user_id)`. Secrets are Fernet-encrypted (invariant
2). The row is not a shareable entity (no visibility columns): it is readable
and writable by its `user_id` only; the owner and admins see that it exists,
never its contents.

**Account-derived configuration.** Some callbacks store what the signed-in
account can reach in `extra_config` (Jira sites, Facebook pages, LinkedIn
organizations, GitHub installations). In `per_user` mode those keys describe
one person's account, so they belong to that person's credential. Each
provider declares them once (`BaseIntegration.account_config_keys`, a
`frozenset` on the provider class, empty by default); the callback writes
them to the credential being stored, and the resolver overlays them on the
integration's `extra_config` for that credential. A provider that declares
none needs nothing.

### The one resolver

`integration_credentials.py` gains the single answer to "which credential does
this integration use for this person":

```python
@dataclass(frozen=True)
class ResolvedCredential:
    source: Literal["integration", "user"]   # which row holds it
    row: Integration | IntegrationUserCredential
    holder_id: uuid.UUID | None              # whose account it is

def resolve_credential(integration, acting_user_id, member_credential) -> ResolvedCredential | None
async def load_credential(db, integration, acting_user_id) -> ResolvedCredential | None
def credential_usable_clause(acting_user_id) -> ColumnElement[bool]   # SQL form
```

| Mode | Acting user | Credential used |
|---|---|---|
| `shared` | anyone | the integration's, when `oauth_credentials_usable` (main's rule, unchanged) |
| `per_user` | the credential's holder (owner, or `oauth_token_owner_id`) | the integration's |
| `per_user` | anyone else who can see it | their `integration_user_credentials` row, else none |
| `per_user` | none (anonymous, owner-less) | none |

`oauth_credentials_usable` / `oauth_credentials_usable_clause` are **not
widened**. They guard readers of the *integration row's* token (the
`GATED_READS` of the tripwire: pollers, element pickers, Test connection, KB
sync, agent-account pipelines); in `per_user` mode `oauth_share_acknowledged`
is `False`, so they already let only the holder read the row. A reader that
must serve a member calls the resolver instead; making the clause accept a
member would hand that member the owner's token through every gated reader.

### Who acts as whom (unchanged)

The design changes *which credential* an identity gets, never *which identity*
a run acts as:

- a chat turn: the person chatting (web, Teams, Slack, Office add-in, MCP
  endpoint, A2A with a mapped user);
- an assignment run: `run.user_id`, else the agent's owner
  (`assignment_engine._acting_identity`; a per-user fan-out sets
  `run.user_id`), passed to the tool build as `context["user_id"]`;
- pollers, webhooks, anonymous channel users, KB sync, skill sources: the
  owning entity's owner (`agent_acting_user_id`);
- REST tooling (Test connection, element pickers, `/integrations/{id}/...`):
  the caller.

Two callers of `build_integration_tools` pass **no** acting user today: the
public widget (`api/v1/public_chat.py`) and the flow runner's integration
nodes (`flow_runner.py`, both calls). They act as the agent's / flow's owner
by the rule above, and must pass it explicitly once a `per_user` integration
can be bound there; with no acting user the resolver returns nothing for a
`per_user` integration, never the holder's token.

So in `per_user` mode an assignment of a shared agent uses the account of the
person it runs as - the behaviour the per-user binding already promises
("its assignments then use the account of the person they run as") - and a
scheduled run of an agent whose owner never connected their account has no
credential, which the run says (below).

**This goes further than the run-time rule.** An agent's or an assignment's
binding is used, for anyone but the agent's owner, only when that person may
use it or the agent lends it (`resolve_agent_integration_bindings`, on chat,
assignment runs and their activation check, evaluations, scheduled work
items); a flow Integration node only when the run's user may
(`shadow_invisible_bindings`); the public widget, an anonymous channel user
and the platform MCP key act as the agent's owner. In `per_user` mode the
funnel substitutes the acting person's credential on every path, because
lending the holder's account to a run acting as someone else is exactly what
the mode refuses.

## Flows

### Connecting one's own account

- `can_authorize_integration(user, integration)` (`api/v1/helpers.py`), the
  rule `start_oauth` and the responses' `can_authorize` share, gains a
  branch: in `per_user` mode, anyone who can see the integration may
  authorize **their own** credential. The owner keeps authorizing the
  integration's own.
- The authorize state carries `credential: "user"`; the callback
  (`_handle_integration_callback`) looks the integration up through the
  visibility filter (not `user_id ==`) for that state, and
  `store_authorized_tokens` writes the tokens and the provider's account
  config to the caller's `integration_user_credentials` row (upsert).
  `mark_authorized` sets that row's status. `acknowledge_share_on_authorize`
  is never called for it, and is a no-op in `per_user` mode for the owner too.
- Everything the sign-in writes today takes the `Integration` and must take
  the credential being stored instead: `store_authorized_tokens`,
  `apply_oauth_token_expiry`, `mark_authorized`, the refresh cooldown reset
  (`clear_refresh_cooldown(refresh_cooldown_key(integration.id))`), and the
  account lookups the callback runs (LinkedIn organizations, Facebook pages,
  Jira sites, `_record_installations_seen_on_connect` for GitHub) which write
  `extra_config_enc`.
- The OpenAPI device-code and implicit grants end in the same helpers
  (`integration_openapi._store_user_tokens`), so they follow once those
  helpers take the credential; `start_device_sign_in` and the implicit
  route call `can_authorize_integration` like `start_oauth`.
- API key: `PUT /integrations/{id}/my-credential` `{api_key}` stores the
  caller's key (validated by the provider's `validate_connection`, as the
  create form does); `DELETE /integrations/{id}/my-credential` disconnects the
  caller. Both audited (`entity_type="integration"`, action
  `credential_connect` / `credential_disconnect`, no secret in details).

### Refresh - the bulk of the work

The whole token lifecycle is written against the `Integration` row, which
holds both the OAuth app (client id / secret, token URL, scopes, the tenant in
`extra_config`, the relay route) and the tokens:

- inline: `_try_inline_refresh` → `_refresh_inline` (lock
  `acquire_refresh_lock(str(integ.id))`, cooldown `_inline_refresh_on_cooldown`,
  `status == "reauthorize"` skip) → `_refresh_inline_locked`
  (`db.get(Integration, integ.id)` re-read, `stored_tokens` compare) →
  `_do_inline_refresh` (row lock `lock_stored_tokens`, writes
  `integ.access_token_enc`, `apply_oauth_token_expiry`, status) →
  `_record_inline_refresh_failure`;
- the build's parallel pass `_parallel_refresh_expiring_tokens`, and the
  closures keyed by **integration id** that re-read the row later in the run:
  `_make_refresh_fn` / `_refresh_by_id` (401 retry),
  `_make_sharepoint_token_fn`, `_make_azure_delegated_token_fn`,
  `_get_extra_config_value`;
- the sweep: `refresh_expiring_tokens` (selects `Integration` rows by
  `status` / `token_expires_at`) → `_refresh_one_integration` →
  `_refresh_stored_integration` → `_do_refresh_integration`,
  `_tokens_still_stored`, `_mark_needs_reauthorization` (sets
  `Integration.status = "reauthorize"`).

None of this can be pointed at a member credential by an overlay: every step
re-reads, locks or writes the `Integration` row by id. The design therefore
introduces one **token store** seam in `token_refresh.py` - which row holds
the tokens (`Integration` or `IntegrationUserCredential`), its lock id, its
cooldown key, its re-read + row lock, its token write, its `reauthorize` /
`expired` mark - while the OAuth app is always read from the integration. The
functions above take the store instead of the row; the closures are keyed by
the resolved credential, not the integration id. A member's failed refresh
then never backs off or marks the owner's credential, or another member's.
The sweep selects expiring member credentials too. (MCP servers already have a
parallel copy of this lifecycle - `_refresh_one_mcp` & co. - the seam is
the way not to add a third.)

### Run time

- The substitution happens **in the funnel**, `_build_integration_tools_inner`,
  right after it batch-loads the bound rows (`_row_cache`), with the acting
  user it reads from `context["user_id"]` - not in `shadow_invisible_bindings`,
  which only chat and scheduled work items call. Every caller of
  `build_integration_tools` (13 today) then gets it.
- `_get_token` and the provider builders read `integ.access_token_enc` /
  `api_key_enc` / `extra_config_enc` on the row they are handed. For the
  *read* side, the funnel hands them a transient, never-persisted view of the
  integration whose credential columns and account config are the resolved
  credential's, so no provider builder changes. Everything that refreshes or
  re-reads goes through the token store above, never through the view.
- `shadow_invisible_bindings` and `resolve_session_integration_bindings` (the
  chat's session selection, which filters on `oauth_credentials_usable_clause`
  and drops the row before the funnel sees it): a visible `per_user`
  integration is kept for a member who has a credential; one who has none gets a
  `missing_integration_bindings` entry with a new reason,
  `own_account_not_connected`, with the integration's name (they can see it).
  `format_unshared_oauth_prompt_note` and the reply's
  `unavailable_integrations` notice say "connect your account" and link to the
  integration page.
- Lending through an agent (`Agent.integration_lending`, #8824): both
  `shadow_invisible_bindings` and `resolve_session_integration_bindings` take
  `lent_ids` (`integration_credentials.lent_integration_ids(agent)`, the lent
  ids the agent still binds) and `lent_owner_id` (the agent's owner). A lent
  id is kept for a member who cannot see it when the row is still the
  owner's and not `per_user`; nothing else reads lending, so the member's
  Integrations list, pickers and bindings (`user_visible_filter`) are
  unchanged. Written by `checked_integration_lending` (owner of the agent and
  of every integration, each bound); an integration share
  (`shared_group_ids`) is a separate, wider grant.
- An assignment run whose acting user has no credential records the same
  entry on the run (the run view shows it), and the run continues without
  those tools, as a chat turn does.

### Changing the mode

| From → to | Effect |
|---|---|
| `shared` → `per_user` | `oauth_share_acknowledged = False`; the integration's token stays, now its holder's only. Audited. |
| `per_user` → `shared` | The members' credentials are **deleted** (they consented to their own use, not to a later lending); the integration's token is lent only with its holder's consent, through `apply_oauth_share_consent` as today. The UI says how many members are disconnected before saving. Audited. |
| any → not shared | Members lose visibility; their credentials are deleted (below). |

**The holder connects too** (superseded at review: see the top of this page). About 25 modules read an integration as usable
only when `Integration.status == "connected"` (session selection, pipelines,
handover prefetch, the agent builder, KB and skill sources, pollers,
`/per-user-readiness`...). That status describes the row's own credential. A
`per_user` integration whose holder never connected would read
`disconnected` / `configured` and vanish from every one of those gates for
every member. Rather than re-spell 25 gates, `per_user` keeps the rule that
the holder (the owner; an admin for a company-managed one) connects their own
account first - which also proves the OAuth app works - and the row's status
keeps its meaning. A member's status lives on their credential. If the
holder's grant dies, the integration reads `reauthorize` for everyone until
the holder reconnects (said on the member's page).

**A change of OAuth app invalidates every grant.** Changing the client id
(`integration_state`, which already clears the row's tokens and
`oauth_token_owner_id`), switching away from OAuth, or the opt-out also
deletes the member credentials: their grants belong to the old app.

`apply_oauth_share_consent()` stays the one write of the consent; the mode is
written by the integration update route, the agent-builder
`update_integration` tool and the import paths, each through one helper
(`set_credential_mode`) that applies the table above.

The `Don't share my connected account` switch becomes the mode choice:

> **Account used by the people this is shared with**
> - Everyone uses my connected account (`shared`)
> - Each person connects their own account (`per_user`)

`keep_oauth_credentials_on_share: false` stays accepted by the REST API with
its meaning on main (strip the token, stay `shared`), so existing clients keep
working; the UI no longer sends it.

### Lifecycle and visibility

- A member credential exists only while its user can see the integration. A
  change of sharing (`shared_group_ids`, `shared_user_ids`, company-managed
  off), a user removed from a group, a deleted user or group
  (`prune_user_grants` / `prune_group_grants`) deletes the credentials of
  users who lost it - one helper, `prune_orphaned_user_credentials`, called
  where visibility changes.
- Deleting the integration cascades.
- Export, clone, templates, the public catalog and skill packages never carry
  a member credential (they never carried the integration's either).
- An owner transfer keeps member credentials (they are their holders', not
  the owner's) and keeps the integration's own token, recording whose it is
  (`record_token_owner_on_transfer`).

## API

- `IntegrationResponse` gains `credential_mode`, `my_credential` (for the
  caller: `null` | `{status, connected_at, last_error}`), and, for the owner
  and admins, `connected_user_count`. For someone acting with their own
  account (`uses_own_credential`), `status` and `has_access_token` are that
  account's (`configured` until they connect it, as any OAuth integration
  before its first sign-in, so their agents can still bind it) and `scopes` /
  `required_scopes` are `null`: the holder's state is never reported to them,
  so every badge and picker reading `status` goes by their own account.
- `PUT /integrations/{id}` (and `POST /integrations`) accept `credential_mode`.
- `GET /integrations/{id}/authorize` serves the member flow under the rule
  above.
- `PUT` / `DELETE /integrations/{id}/my-credential` (API key; `DELETE` also
  disconnects an OAuth credential).
- `GET /integrations/{id}/credentials` (owner, admins): who is connected and
  their status - names and dates, no secret - and
  `DELETE /integrations/{id}/credentials/{user_id}` to disconnect someone.
- `/per-user-readiness` counts a visible `per_user` integration the user is
  connected to as their own instance.
- The agent builder `update_integration` tool takes `credential_mode`, with
  the same helper.

## UI

- Integration Configuration card: the mode choice above, shown once the
  integration is shared; the connected-member count and list (owner, admins).
- Integration page, for a member of a `per_user` integration: a **Your
  account** block - Connect (Authorize, or an API-key field), status,
  Disconnect.
- Integrations list: a badge "Your account: connected / not connected" on
  `per_user` rows.
- Chat: the `unavailable_integrations` notice links to the integration page
  for `own_account_not_connected`.
- Sharing preview: `per_user` is described as "each person's own
  account" (`resource_exposure.describe_secret`).

## Security

- A member credential is used only for its holder: the resolver is the only
  reader (tripwire test extended to `IntegrationUserCredential`'s secret
  columns, same `GATED` / `BOUND` / `EXEMPT` lists).
- The OAuth state already binds the flow to the user who started it; the
  callback writes to that user's row only, so a crafted state cannot land a
  token on someone else's credential.
- An admin can see and remove a member's connection, never use it: there is
  no "act as" path. Admins use their own credential like anyone else in
  `per_user` mode.
- The integration's outbound guards (relay, `allowed_destinations`, the
  OpenAPI server-origin rule, token URL SSRF guard) are configuration and
  apply to every credential.

## Migration

One Alembic revision (invariant 13: idempotent, `terminate_lock_holders()` +
`set_lock_timeout()`):

1. `integrations.credential_mode` `VARCHAR(16) NOT NULL DEFAULT 'shared'`
   with a check constraint.
2. `integration_user_credentials` and its unique index.

No data change: every row stays `shared`, which is main's behaviour. A shared
OAuth row that is currently withheld (shared, unacknowledged, no token) keeps
that state; its owner moves it to `per_user` from the card.

## Where it touches the code

| Area | Change |
|---|---|
| `models/integration.py`, `models/integration_user_credential.py`, `models/__init__.py` | column, new model |
| `services/integration_credentials.py` | `resolve_credential`, `load_credential`, `set_credential_mode`, `prune_orphaned_user_credentials`, new withheld reason |
| `services/integration_tools.py` | substitution in `_build_integration_tools_inner`; the inline refresh chain and the id-keyed closures (`_make_refresh_fn`, `_make_sharepoint_token_fn`, `_make_azure_delegated_token_fn`, `_get_extra_config_value`) on the token store; `shadow_invisible_bindings` |
| `services/token_refresh.py` | the token store seam: lock, cooldown, re-read, row lock, write, `reauthorize` mark; the sweep selects member credentials |
| `api/oauth_callbacks.py`, `api/v1/integration_openapi.py` | `store_authorized_tokens`, `apply_oauth_token_expiry`, `mark_authorized`, cooldown reset and the account lookups target the credential being stored; device code and implicit grant |
| `services/integration_state.py` | a new OAuth app deletes the member credentials |
| `api/v1/public_chat.py`, `services/flow_runner.py` | pass the acting owner to the tool build |
| `services/integration_credentials.py` (`resolve_session_integration_bindings`) | keeps a `per_user` integration for a connected member |
| `api/v1/integrations.py`, `api/v1/helpers.py`, schemas | mode, `my-credential`, `credentials`, `can_authorize_integration` |
| `integrations/base.py` + the providers that store account config | `account_config_keys` |
| `services/kb_sync.py`, `services/skill_source.py`, `services/event_poller.py`, `services/flow_triggers.py`, `api/v1/integration_elements.py`, `api/v1/integration_notifications.py` | read through the resolver for their acting user |
| `services/resource_exposure.py`, `builtin_tools/agent_builder.py` | mode description, `update_integration` |
| `frontend/src/pages/integration-detail/`, `lib/oauth-share.ts`, hooks, types, i18n | mode choice, Your account block, list badge, notice link |
| `tests/test_integration_credential_gate.py` | extended to the new secret columns |
| docs | `docs/docs/` integrations sharing page; `dev-docs/reference/integrations-and-knowledge-bases.md`; `dev-docs/reference/backend-integrations-model-and-oauth.md` (the personal-token paragraph) |

## Tests

- Resolver table above, both forms agreeing (row and clause), as a non-admin
  member, the owner, a token holder after transfer, an admin, anonymous.
- Callback: a member's authorization lands on their credential, never on the
  integration; the owner's re-authorization in `per_user` mode never sets
  `oauth_share_acknowledged` (the reported case, as a regression test).
- Run time: two members chat with the same shared agent and each call carries
  their own token (respx); a member without a credential gets the notice; an
  assignment uses `run.user_id`'s credential, else the agent owner's; a flow
  node and the widget use the owner's, never the holder's for someone else.
- Refresh: a member's refresh writes their row only; cooldowns are
  independent; the sweep refreshes member credentials.
- Lifecycle: unsharing, group removal, user deletion, `per_user` → `shared`
  delete the right credentials.
- OpenAPI: SaaS Manager-shaped spec (authorization code), device code and
  implicit grants in `per_user` mode.
- API key: a member's key is used for them, the owner's for the owner.
- Frontend: the mode choice, the Your account block, the notice link
  (`yarn check`).

## Delivery

Two PRs on one issue, each shippable:

1. **OAuth** - mode, table, resolver, callback, refresh, run time, UI. Fixes
   the reported case for every OAuth provider, OpenAPI included.
2. **API keys** - `my-credential`, the API-key field in the Your account
   block.

## Alternative considered: a member connection as its own `Integration` row

Each member's connection could be a hidden personal `Integration`
(`member_of_id` → the shared one) holding a synced copy of the configuration.
Its draw: the whole token lifecycle above (refresh, locks, cooldowns,
`reauthorize`, the callback's `user_id ==` lookup, device code, account
lookups, status gates) works on it unchanged, since it is an ordinary row
owned by the member. Rejected because:

- 76 `select(Integration)` queries would see the member rows: lists and
  pickers, pollers, Slack delivery routing (`slack_events._receiving_integrations`),
  GitHub installation routing, the sharing view, exports, counts. Each would
  have to exclude them, and a missed one shows or uses a member's connection
  where it does not belong; a new table is invisible to all of them by
  construction.
- The configuration lives in ~12 columns plus other tables keyed by the
  integration id (the imported OpenAPI spec and its tool selection,
  `scope_config`), so the copy drifts unless every write path of the shared
  integration re-syncs it.

The cost of the chosen design is the token store seam, contained in two
modules and pinned by the credential-gate tripwire.

## Open questions

1. `per_user` → `shared` deletes the members' credentials. Keep them dormant
   instead (restored on a switch back)? Deleting is simpler and keeps no
   token nobody uses; dormant saves members a re-authorization.
2. The run-time exception (above): agreed by the maintainers?
3. Should the owner be able to require `per_user` (members cannot be lent the
   owner's account even by an admin)? Today an admin may consent on the
   owner's behalf (`may_consent_to_share`).
4. GitHub: webhook routing and the GitHub App installation records
   (`webhooks.py`, `_installer_integrations`) read the integration row's
   token. In `per_user` mode they keep using the holder's - events act as the
   agent owner anyway. Acceptable for v1?
5. Scheduled runs of an agent whose owner never connected their account:
   fail the run, or run without the tools and say so (proposed)?
