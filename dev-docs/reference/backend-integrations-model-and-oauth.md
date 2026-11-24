# Integrations: model, credentials and OAuth

What an integration is, its stored columns, the personal OAuth token and per-person credentials, and the OAuth flow (GitHub App installs included); the rules live in `backend/app/integrations/AGENTS.md`.

## What is an Integration?

An integration is a connection to an external service (Google, Microsoft, GitHub, Slack, etc.). Integrations provide credentials (OAuth tokens or API keys) that agents use during assignment execution to call external APIs.

**All credentials are stored per-integration in the database, encrypted with Fernet. No integration credentials live in `.env` or environment variables.** Users configure everything through the web UI.

Integrations support `is_company_managed` — when true, the integration is shared with all users and only admins can edit. When toggling from company-managed to private, the backend automatically removes the integration from other users' agent bindings.

## Integration Model

Each integration stores all credentials encrypted in the `integrations` table:

| Field | Storage | Purpose |
|-------|---------|---------|
| `description` | Plain | Optional free-text description — surfaced to agents as context for integration tools |
| `client_id_enc` | Fernet | OAuth app client ID |
| `client_secret_enc` | Fernet | OAuth app client secret |
| `access_token_enc` | Fernet | OAuth user access token (set after OAuth flow) |
| `refresh_token_enc` | Fernet | OAuth user refresh token |
| `api_key_enc` | Fernet | API key or bot token |
| `extra_config_enc` | Fernet JSON | Provider-specific (e.g. `phone_number_id`, `signing_secret`) |
| `authorize_url` | Plain | OAuth authorize URL |
| `token_url` | Plain | OAuth token URL |

**An OAuth token is personal**. Only the owner may *use* an OAuth integration's `access_token_enc` / `refresh_token_enc`, unless the owner acknowledged the share (`oauth_share_acknowledged`). Sharing the integration (group, per-user grant, company-managed) from its Configuration card records that consent by default, after its notice - once shared, everyone it is shared with uses the connected account - unless the owner opts out (`keep_oauth_credentials_on_share: false`, the `Don't share my connected account` switch), which keeps it shared without the account: the owner keeps their token, and everyone else authorizes their own (below). Through the REST API only an explicit `true` records it: a share that sends no choice opts out of sharing an unconsented account (`requested_share_choice()`), and an edit that changes no sharing leaves the consent as it is. Only the account holder may consent (`may_consent_to_share()`: the owner, the token's recorded owner, an admin). `integration_credentials.apply_oauth_share_consent()` is the one write of the consent; visibility still decides who can reach the integration at all. Work run on behalf of an agent (polls, webhooks, the public widget, anonymous channel users) acts as the agent's owner. The rule applies where a person chooses a credential - binding a KB sync source or a skill source (the other binding writes refuse an integration the writer cannot see), the REST tooling, a chat turn's session selection, per-user resolution, the event pollers - through `app/services/integration_credentials.py`: `oauth_credentials_usable_clause()` in a query, `oauth_credentials_usable()` on a loaded row, `require_oauth_credentials_usable()` (403) in a REST route. It never applies at run time: a credential a KB sync source or a skill source is bound to is used by its runs as bound, through `build_integration_tools()`. An agent's bindings and a flow Integration node are used only by a run whose person may use them, or that the agent lends (`resolve_agent_integration_bindings()`, `shadow_invisible_bindings()`), and a run with no person keeps them only when it is the agent owner's own (`owner_run`). A new read of either column fails `tests/test_integration_credential_gate.py` until it is listed there, gated or bound. A refusal is never silent: the 403 detail (`OAUTH_CREDENTIALS_NOT_SHARED_DETAIL`) says how the token gets shared (saving the configuration with `Don't share my connected account` off), and what a chat turn does not use for its user - a session selection the rule narrows, an integration they cannot see, a non-owner's shadowed binding nothing replaced - is told to the model (`format_unshared_oauth_prompt_note()`) and shown under the reply (`unavailable_integrations`). The OAuth callback records the acknowledgement when a shared integration is authorized by its owner (or an admin, company-managed; the Authorize button says so), admins keep the REST tooling, and migration `b8ack2642001` acknowledged the shared OAuth integrations that already held a token. An owner transfer keeps the stored token, which is then not the new owner's: `integration_credentials.record_token_owner_on_transfer()` records who connected it (`oauth_token_owner_id`, cleared by the owner's own authorization, the share opt-out, a new OAuth app and a switch away from OAuth), and the integration page says so (`oauth_connected_by_other`) so the owner can reconnect with their own account - nothing is refused.

**Shared without the connected account: each person's own.** The share choice is stored: `apply_oauth_share_consent()` writes `Integration.credential_mode` with the consent - `shared` (the account is lent, consent recorded) or `per_user` (the opt-out, `keep_oauth_credentials_on_share: false`, the `Don't share my connected account` switch, and the REST baseline for a share that makes no choice). In `per_user` the owner's own token is kept and is theirs only (a token a previous owner left, is removed), re-authorizing never lends it (`acknowledge_share_on_authorize` is a no-op), an implicit consent never ends the opt-out, only an explicit `true` does; and everyone else who can see the integration authorizes their own account on it, an `IntegrationUserCredential` (`models/integration_user_credential.py`, OAuth tokens only). `services/integration_user_credentials.py` is the one module: `resolve_token_holder()` picks the row holding the credential for an acting person (the integration for its holder - `oauth_token_owner_id`, else the owner - their own credential for anyone else who can see it, nothing otherwise: never the holder's for someone else, nothing with no acting person); `apply_share_choice()` wraps the consent write and disconnects the people's accounts when the account is lent again or the integration stops being shared. Every reader goes through the resolver: the tool funnel substitutes the holder after loading the bound rows (`_build_integration_tools_inner`; the acting person is `context["credential_user_id"]`, which the widget and the flow runner set to the agent's / flow's owner, else `context["user_id"]`) - for this mode the exception to "used as bound", on every path; the chat's `shadow_invisible_bindings` and `resolve_session_integration_bindings` keep the integration for a connected member and otherwise report `own_account_not_connected` (the reply links to the integration page); the REST tooling uses `usable_credential_holder()` (no admin bypass: `require_oauth_credentials_usable` lets nobody but the holder read the row in this mode, admins included); the pollers, Graph notifications and agent accounts `reader_clause()` + `resolve_token_holder()` for the owner they act as; a KB sync source the source owner's; a skill source refuses the mode (shared and unattended); a per-user (template) binding resolves to a company-managed `per_user` integration for a person who connected their own account on it (`template_candidate_clause`). The credential row is a **token holder**: it holds its tokens, expiry, status and `account_config_enc` (what the sign-in learned - Jira sites, Facebook pages, LinkedIn organizations, GitHub installations: the catalog's `account_config_keys` - plus any other key it changed, `save_account_extra()`; the integration's own account keys are never read through it, `configuration_extra()`), and reads every other column from its integration through generated read-only properties, so `_get_token`, the inline refresh and the sweep run on it unchanged. Its lock and back-off are its own (`holder_ref()` = `user-credential:<id>`; an integration's stays its bare id), and the steps keyed by the integration's identity - the client secret and its vault record, the relay route, the client certificate - read `integration_of(holder)`. A sign-in by anyone but the holder (`signs_in_own_account()`) carries `credential: "user"` in its OAuth state and lands on that person's credential, leaving the integration's tokens, scopes and consent alone; the OpenAPI device code and implicit grant follow the same rule. The holder need not connect first: the gates on an acting person's path read `connected_for_clause()` (the row is connected, or that person's own credential is) and a resolved holder's status `holder_connected()`. The integration's response shows such a person their own account (`status`, `has_access_token`; no scopes), never the holder's (`_to_response`). A new OAuth app deletes the people's credentials; a sharing change prunes those of people who lost sight of it (`prune_unreachable_user_credentials()`, and the resolver re-checks visibility at use). Migration `u5545ucred02` stored the opt-out of the integrations already shared without their account.

## OAuth Flow (per-integration credentials)

1. User creates an integration in the UI, pastes their OAuth app's client_id + client_secret
2. Integration status changes to "configured"
3. User clicks "Authorize via OAuth" → `GET /api/v1/integrations/{id}/authorize`
4. Backend reads client_id from the integration's encrypted DB row
5. Backend generates state token, stores `{user_id, integration_id}` in Redis (5 min TTL)
6. Backend returns the authorization URL
7. Frontend redirects user to the provider
8. Provider redirects to `GET /api/oauth/callback?code=...&state=...`
9. Backend loads the integration from state, exchanges code using integration's own client_id/client_secret
10. Tokens are Fernet-encrypted and stored on the same integration row
11. Integration status changes to "connected"
12. Backend redirects back to the integration's page, where the sign-in started: `{FRONTEND_URL}/integrations/{id}?connected={provider}` (`?error=token_exchange_failed&provider={provider}` when the exchange fails; `_integration_page_redirect`). The path is built from the loaded row's id and the query is encoded, never text from the state; an own-account sign-in and the implicit grant land there too, and the page toasts the outcome and drops the query

A GitHub App installed from GitHub's own UI redirects to the same callback with
`code` + `installation_id` + `setup_action=install` and **no** `state`, so
nothing names the XTM One user or the integration (`_handle_github_app_install`).
The code is exchanged only to ask GitHub whose account installed
(`GET /user`) and whether it sees the installation (`GET /user/installations`).
The installation is then recorded on the `connected` integrations of the same
app whose own token answers that same GitHub account and that already record
installations - read again `FOR UPDATE` once GitHub has answered, each write
audited under `system:github_app_install` - never on another user's, and never
from the query string alone (a non-numeric `installation_id` is refused before
GitHub is asked). No token is stored and no integration is connected: a
waiting, `error` or `reauthorize` integration is connected with its own Connect
button (step 3), whose `state` names it.

Around that proof: the callback is rate limited through
`core/rate_limit.py` per client address and per installation (429 +
`Retry-After`, before GitHub is asked); a row whose token changed during the
proof (a refresh) is proven once more with its new token, and every row left
alone is logged with the reason; the audit entry of an owner-less
company-managed row is anchored on whoever connected its token, else the
oldest admin (`_install_audit_anchor`); the redirect names no integration,
since the browser's user is unknown. Under "Require verified integration
notifications" (`admin_app_scoping_required()`) the Connect callback also adds
the installations its new token sees to a GitHub integration that already
records installations (`_record_installations_seen_on_connect`), so an
integration connected with a bot account learns of an installation someone
else made; with the option off it asks GitHub nothing more.
