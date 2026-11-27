# Browser profiles

A browser profile is a signed-in browser identity for one web application:
its start URL, how the platform's headless Chromium signs in to it
(credentials, a TOTP secret, interactive MFA) and the session that sign-in
saved. It shipped inside the Video Studio and became a platform
entity so every browser of the platform opens it:

| Browser | Opens a profile when | Module |
|---|---|---|
| The agent `browser` tool (chat, assignments, sub-agents, flows) | the agent binds it (`Agent.browser_profile_ids`) and the run's person may use it | `agent_access.py`, `agent_sessions.py`, `builtin_tools/web.py` |
| The General Assistant's `browser` tool | the person gave it to their assistant (`User.ga_browser_profile_ids`) | same |
| A Video Studio recording | the take's person may use it (no binding: a scripted job) | `video_studio/capture.py` |
| The sign-in, the session check, the keep-alive | the caller may use it / the worker | `signin.py`, `keepalive.py` |

Everything lives in `services/browser_profiles/`, the model in
`models/browser_profile.py` (table `browser_profiles`, the three-tier
visibility columns of `models/visibility.py`). The Video Studio depends on the
package, never the reverse. No feature flag: the API, the page and the agent
surface work with the Video Studio off.

## Who may open what (`agent_access.py`)

The ONE rule every place that builds the `browser` tool reads
(`agent_chat`, `assignment_engine`, `general_chat`):

- **Bound.** An agent opens only the profiles in `browser_profile_ids`; a
  person's General Assistant only those in `ga_browser_profile_ids` (no
  platform default: a profile is a login, given by its user). Binding is
  explicit, so an agent never reaches every login its users can see.
- **Usable by the run's person.** The three-tier predicate WITHOUT the admin
  bypass (`profiles.list_usable_profiles`), for the person the run acts for:
  the chatting user, an assignment run's `run.user_id` (its execution
  identity, never the file owner). A run that acts for nobody - an anonymous
  public widget visitor, an ownerless company-managed run - opens none.
  Binding a profile therefore lends it to nobody beyond its own audience,
  which is why `resource_exposure` has no browser profile arm.
- **Read-only bindings.** `browser_profile_read_only` (a subset of the bound
  ids, `normalize_read_only`) opens the profile read-only: the tool refuses
  `click`, `type`, `press`, `hover` and `evaluate` in that session
  (`builtin_tools/web.BROWSER_INTERACTIVE_ACTIONS`); `viewport`, `console`
  and a `screenshot` of one element only read the page and stay allowed. A GET link can still
  change state in an application that does so on a simple visit; that is
  stated in the user docs.

Writes are checked where they are written: `api/v1/agents.py`
(`_checked_browser_profile_ids`, `binding_access.refused_additions` with
`is_admin=False`; a company-managed agent binds company-managed profiles
only; ids already bound are never re-checked) and `PUT /settings/preferences`
(`_checked_ga_browser_profile_ids`). Going private removes a profile from
other people's agents (`handle_company_managed_toggle(agent_field=
"browser_profile_ids")`). A copy keeps both lists (same instance); an export,
a public download and a catalog clone carry neither (`agent_copy.py`).

## The agent session (`agent_sessions.py`, `browser_tool.py`)

The tool gains the `profiles` action and `navigate`'s `profile` argument only
for a run with bindings (`build_browser_tool(profile_access=...)`); the
schema and description of every other run are unchanged.

- **Opening.** `navigate` with `profile` resolves it among the usable bound
  profiles (id, or exact case-insensitive name; a duplicate name asks for the
  id), then `open_profile_session` launches `browser.profile_browser` on the
  saved session under the profile's URL policy and runs
  `signin.ensure_signed_in` - the probe of every other use, with a sign-in
  again when the profile is `renewable`. A profile that needs a person (an
  MFA code without a TOTP secret, a push) is refused with what to ask of
  them: the agent loop has no channel for the prompt (the Video Studio tools
  keep `sign_in_browser_profile` / `submit_browser_mfa_code` for that). A
  profile with no session and no way to sign in is refused before any
  browser launches. The session replaces the conversation's current one
  (`browser_tool.install_session`, key `{agent}:{conversation}`); the next
  actions run in it until `close` or another profile.
- **URL policy.** `BrowserSession.check_url` carries the profile's policy
  (`browser.check_target`): the navigate gate, the navigation-race re-reads
  and the redirect guard use it, so a profile's declared private hosts are
  reachable and nothing else is.
- **Audit.** Every opening writes `browser_profile` / `use` as the run's
  person, with the agent, `via` (`agent` | `general_assistant`), whether it
  was read-only and whether it signed in again.
- **Write-back.** The session's `release` (the `close` action, the
  five-minute idle sweep, opening another profile) reads the refreshed state
  (`signin.save_session`: read, then probed in a fresh context) and saves it
  only when the probe still renders the application - an agent that signed
  out never erases the saved session - and only when nobody saved a newer
  one: `store_refreshed_session` row-locks the profile and compares
  `session_saved_at` with the value the session loaded. A person signing in
  again, the keep-alive or another run therefore always wins over an older
  agent session. Two agent sessions of one profile whose application rotates
  its refresh token with reuse detection can still invalidate each other;
  the compare-and-set keeps the stored session the newest valid one.
- **Saving the browser's session into a profile.** The
  `save_session` action (offered with the `profiles` action, `profile` = the
  profile to save into) keeps a session the agent established with the plain
  `browser` tool (an SSO the person completed in the conversation, an MFA
  code they gave) as the profile's session:
  `browser_tool.capture_session` reads the open session between two actions
  (never opens one) and `agent_sessions.save_browser_session` decides. The
  profile resolves among the usable bound profiles (`resolve_profile`), the
  binding is not read-only, the run's person may manage it (`can_manage`, as
  every other write to a profile), the session is not signed in with another
  profile and its page is on one of the profile's hosts (`profile_hosts`:
  start URL, sign-in URL, `allowed_hosts`). Only the cookies and origins of
  those hosts are kept (`scoped_state`; a cookie of a parent domain of a host
  counts, `sessionStorage` only from the start URL's origin): nothing of the
  other sites the agent browsed. The probe of a sign-in (`probe_profile`: the
  start URL in a fresh context holding exactly that state, under the
  profile's URL policy) decides: rendered, the state is saved with the probe
  (status `active`, MFA challenge fields cleared, `last_sign_in_message`
  names the agent) under a row lock and replaces any older session - a
  person chose to save it; not rendered or not answered, nothing is written.
  The capture reads IndexedDB too (`storage_state(indexed_db=True)`, as a
  sign-in saves it), so an application keeping its session there is probed
  signed in. A save from a session opened with that same profile moves the
  session's write-back mark (`SessionMark`) to the save's
  `session_saved_at`, so its write-back on release is not refused as older.
  Each save, saved or not, writes `browser_profile` / `update` as the
  person with `saved` and the probe's reason. One-off credentials for
  `sign_in_browser_profile` given in the chat are not part of it: the
  posture that the assistant never takes a credential in the chat stands.

## Sign-in, MFA and sessions (`profiles.py`, `signin.py`, `core/totp.py`)

A profile is shared like any entity (three tiers; using one needs the
visibility predicate WITHOUT the admin bypass, managing it `can_manage`).

- **Credentials** (`username`, `password`, `totp_secret`) are one
  Fernet-encrypted JSON (`credentials_enc`), write-only: serializers expose
  `has_*` flags and a masked username, never a value. They are written only
  by the REST API (the Browser profiles page): no tool takes one - a
  tool call's arguments stay in the conversation and its traces -, so
  `save_browser_profile` writes the URLs and the sign-in settings and tells
  the agent to ask the person to type the credentials on the page.
- **Credentials read from a secret vault.** Each credential may be a
  `vault://<provider>/<path>#<key>` reference instead of the value, picked on
  the page with the platform's `SecretField` (a 1Password item's password and
  one-time password field included, through the 1Password vault provider).
  `browser_profiles.credentials_enc` is a `REFERENCE_COLUMNS` entry like every
  secret field: the write is checked (`vault_reference_validator`,
  `require_vault_writer` with the caller's grants), the profile's start URL,
  sign-in URL or private hosts move only under the re-point rule
  (`profiles.repoint_problem`, asked by the REST route AND
  `save_browser_profile`: a reference the editor could not have written stays
  only if the same edit replaces it), the where-used scan, the delete guard,
  the Security Audit and the failure record cover it, and responses carry
  `vault_credentials` (names) and `vault_secrets` (where, the path only to a
  manager who may use it). **Every browser task takes
  `profiles.resolved_snapshot()`** - the sign-in, the dry run, the session
  check, the agent `browser` session, a recording, the keep-alive - which
  reads each reference through the one resolver (`app.services.vault`) when
  it is used; `snapshot()` (status flags only) names a reference in
  `referenced` and never puts it in `credentials`, so a browser can never type
  `vault://...` into a form. A credential that cannot be read is left out and
  named in `credential_failures` (recorded on the profile, shown on its card):
  the sign-in fails at once saying so, the agent session and the keep-alive
  name it, and nobody is told to ask a person to sign in. A TOTP secret read
  from a vault is checked when read (`INVALID_TOTP` otherwise); its status is
  `vault`. **Who may read it**: the platform's vault identity resolves the
  reference for whoever may USE the profile (three tiers, no admin bypass,
  the agent binding rules below), as for any entity's secret; the vault's
  grants decide who may WRITE or re-point the reference, never each read.
  `tests/test_vault_secret_readers.py` fails the build if code outside
  `profiles.py` reads the stored credentials or the plain snapshot.
- **URL policy of every request.** The route filter of a profile
  browser context (`browser.RouteFilter`) holds the first URL of every
  subresource, XHR, frame and worker request to `check_target`: a literal
  address at once, a host name resolved once per host for the life of the
  context (concurrent requests share the resolution, a failed check refuses
  the host). The redirect guard holds every hop after it.
- **TOTP** (`app/core/totp.py`, RFC 6238, stdlib - the platform's one TOTP
  implementation, the 1Password integration's `get_onepassword_otp` computes
  with it too): a base32 secret (spaces, dashes and
  lowercase accepted, padding optional) or an `otpauth://totp` URI (algorithm,
  digits, period honoured); a code with less than 3 s left is replaced by the
  next window's. The secret is validated on save AND at use. A
  `vault://` reference is a credential read from a vault (above). Any other
  pointer INTO a secret manager (`op://`, `${...}`, any non-`otpauth`
  `scheme://`) is refused with an error naming the shape and the way to read
  it (connect the secret manager as a secret vault and pick the one-time
  password from it), never stored as if it were base32.
  `totp_secret_status` is `none` | `valid` | `invalid` | `vault`, on the
  profile status and in `list_browser_profiles`.
- **Per-profile bounds** (migration `v5961probe01`): `sign_in_timeout_seconds`
  (30-600, 120), `session_save_timeout_seconds` (5-120, 30) and
  `mfa_timeout_seconds` (60-900, 300), `profiles.TIMEOUT_BOUNDS`.
- **Reading the page.** `signin.classify` reads structure only - no
  text phrase lists, invariant 18 - the same way on a first-party form and on
  a hosted identity provider (SAML / OIDC): a password field; a one-time-code
  field (`autocomplete="one-time-code"`, a numeric input of 6-8 characters,
  one box per digit; the profile's `otp_selector` and name hints after
  those); a large 1-3 digit number on a page without fields (number match);
  unticked checkboxes and the default button of their form (a "stay signed
  in" / "don't ask again" interstitial: ticked, then accepted); a form of
  hidden fields posting to another site (a SAML ACS relay: posted when it
  does not post itself); a meta refresh. Every read is bounded
  (`_READ_TIMEOUT_SECONDS`): a script waits while a navigation is in flight.
- **Passkey / FIDO bridge.** Headless Chromium has no authenticator, so
  a WebAuthn ceremony never completes. `browser._BLOCK_WEBAUTHN_JS` (an init
  script on every profile context) makes `navigator.credentials.get` reject at
  once and counts the attempt, so an identity provider that defaults to a
  passkey / security key does not hang on its bridge page. `signin._fido_bridge`
  reads that page by STRUCTURE - a fieldless page whose path is a FIDO bridge
  (`/bridge/fido`) or that tried `navigator.credentials.get` - classifies it as
  an UNUSABLE method (never a push), and backs out deterministically:
  `_back_out_of_fido` navigates to the decoded `cancelUrl` query parameter
  (no DOM dependency), else clicks "Sign in another way" / the back arrow
  (`#signInAnotherWay`, `#idA_PWD_SwitchToCredPicker`, `#idBtn_Back`). If it
  cannot leave, it fails naming the method rather than waiting it out.
- **Credential picker.** On "Verify your identity" / "Choose a way to
  sign in", `_choose_picker_method` picks a method that yields a code the
  platform can type - an authenticator code (`data-value="PhoneAppOTP"`), then
  SMS (`OneWaySMS`) - over a push (`PhoneAppNotification`), which it chooses
  only when no code method exists. The interstitials on the way back are
  handled by their stable ids: "Stay signed in?" (`_accept_kmsi` ticks
  `#idChkBx_SAOTCC_TD`, then clicks `#idSIButton9` = Yes, never "No"), and a
  "More information required" / proof-up page (`_skip_proof_up`).
- **Password refusal.** "Credentials refused" is reported ONLY when the
  password form still shows its error banner (`#passwordError`, or a visible
  alert) after the password was filled on that same page; a different URL with
  no visible error is progress, not a refusal, so a sibling app further down the
  redirect chain is no longer mislabelled.
- **A trace of the decisions.** Every decision is logged and kept on the
  outcome's `trace` (`{stage, url, decision, at}`: `password_submitted`,
  `fido_bridge_bypassed`, `picker_choice=PhoneAppOTP`, `totp_submitted`,
  `kmsi_accepted`, ...), so an operator sees why a sign-in stalled.
- **Interactive MFA.** A code prompt or a push without a TOTP secret
  publishes a challenge (Redis `video_studio:sign_in:{id}:state|codes`, a
  local fallback without Redis): `status="mfa_required"`, `kind` `code` |
  `push_number` | `push`, the screenshot, the number to match, `expires_at`.
  The browser stays open on the prompt for `mfa_timeout_seconds` (300);
  `submit_browser_mfa_code` (or `POST /mfa/{id}`) types the code into that
  same context. A second `sign_in_browser_profile` while a challenge waits
  returns it. With a TOTP secret the code is computed; a refused one is
  retried once with the next time step's code (`totp.current_counter` + 1).
- **Completion = the application, not the IdP.** The flow is done when the
  browser is back on `start_url`'s host and `check_authenticated` passes:
  `start_url` opened (the redirect chain followed until the main frame rests,
  `_follow`), then judged: `signed_out_url_pattern` refuses; a
  `signed_in_selector` on the page passes (absent after its 8 s wait, the
  checks below decide without it: a page they pass is signed in, its reason
  saying the selector is stale, update or clear it; a page they refuse fails
  naming the selector, `_past_stale_selector`, #10002); a password /
  one-time-code field refuses;
  `signed_in_url_pattern` decides; another host refuses (the IdP); the
  profile's `login_url` refuses (unless `login_url` is `start_url`'s own page:
  a single-page application serving its sign-in form and its dashboard at
  one URL, its session a `localStorage` token, is judged by the comparison
  below, #10027); and the page must not render what
  `start_url` renders in a fresh context with no session
  (`signed_out_render`, memoised per browser; the failure names the signal:
  same URL, title, fields and amount of content). A saved state that kept
  every `localStorage` key the page held is a saved session without any
  cookie (`persistence_note` says "session saved", never "not persisted"). The application's own SSO
  page - no field, on its own host - is therefore never read as signed in.
- **An unavailable server is not a signed-out page.** `_Navigation` keeps the
  status the server answered the current document, and the first 5xx / 408 /
  425 / 429 of a script or stylesheet it loaded. `_follow` sets
  `PageState.http_error` from them (an asset's status only when the page
  renders nothing); `_open_start` then opens `start_url` again after
  `_UNAVAILABLE_BACKOFF_SECONDS` (3, 8, 15 s; a `Retry-After` lengthens a wait,
  up to 30 s, `_retry_wait`). The waits of one check - its page and the
  signed-out render it compares with - share `_UNAVAILABLE_BUDGET_SECONDS`
  (30 s from the check's start): a wait ending past it is not taken, and
  `POST /browser-profiles/{id}/check` is bounded by `CHECK_TIMEOUT_SECONDS`
  (90 s, then 503) - the probe and the wait for an unattended renewal share it
  (`start_sign_in` waits only for what is left; past it the profile answers
  `signing_in` and the renewal goes on in the background). A state still carrying `http_error` fails `_judge` with that
  status (never the "without a session" verdict), and a signed-out render
  that does is neither compared nor memoised. A reverse proxy's error page
  is otherwise "the same page as without a session" when the signed-out
  render was refused too, and "the application" when it was not.
  `classify` also reads the page's own load (Navigation Timing
  `responseStatus` of the document, Resource Timing of its same-origin
  scripts and stylesheets), so `_marked` refuses such a page even where no
  `_Navigation` watched the load (`wait_signed_in` in marks mode, which reloads
  it after the same waits). The probe carries `ProbeResult.http_status`:
  `record_probe` then keeps the session `unverified` (never `expired`);
  `ensure_signed_in`, the keep-alive, the session check (route and
  `check_browser_profile_session`, whose `session_check` carries
  `http_status`) do not sign in again; a refreshed session whose re-probe was
  refused is still saved, `unverified` (keep-alive, agent write-back), and a
  sign-in (or a dry run) whose final probe was refused ends with the outcome
  `status="unverified"` (`_saved_session_status`, no stage), never `failed`; the
  errors read `profile session not checked` (`signin.session_error_text`, one
  text for the recording and the agent `browser` session, whose hint is
  `UNAVAILABLE_HINT`, never "ask the person to sign it in"). A recording
  stopped so is queued again, never failed: `runners.run_take` answers
  `hold` and the take adapter defers it, no attempt spent, for
  `unanswered_wait` (30 s doubling to 5 min), failing it with that message
  once it waited `UNANSWERED_HOLD_SECONDS` (30 min) - a deploy or a restart
  outlasts the probe's own retries.
- **Stages and timeouts.** `sign_in_timeout_seconds` (120) is counted from
  the last main-frame navigation (a navigation request starting, a commit, a
  new view of a single-page IdP), never from the start;
  `session_save_timeout_seconds` (30) bounds reading and writing the
  session. A timeout is `SignInTimeoutError(stage, last_url)`, the stage one
  of `idp_form`, `mfa`, `redirect_chain`, `app_load`, `session_save`; a refused
  authenticator code fails with the stage `mfa_totp_refused`.
- **Unattended renewal (the default).** `renews_by_itself`
  (status + list, `profiles.renews_by_itself`) reads ONE computation,
  `snapshot.renews_unattended`: true when the profile can finish a sign-in
  with no person: credentials + a VALID TOTP secret; credentials whose last
  sign-in, dry run or keep-alive renewal reached the application asking no
  person (`BrowserProfile.signs_in_unattended = True`, a local login with no
  MFA); credentials no sign-in has tried yet (`None`, unless a stored TOTP
  secret cannot compute codes); or a saved identity-provider session to
  replay. A sign-in that asks a person for a code or an approval sets the flag
  `False` (the dry run and the keep-alive too, on `SignInRequiredError`); new
  credentials (`write_credentials`) reset it to `None`. Only a DRIVEN sign-in
  (`SignInResult.driven`) teaches it: a session already signed in says nothing
  of the credentials. `check_browser_profile_session` (REST and tool) and the
  recording pre-flight renew the session by themselves on expiry when that holds,
  and report the outcome.
- **Dry run.** `dry_run_sign_in` runs the whole sign-in once WITHOUT
  saving anything (the live session is untouched), and returns the trace and a
  screenshot of where it ended - behind `POST /browser-profiles/{id}/test-sign-in`
  and the `test_browser_profile_sign_in` tool, so an operator proves a profile
  signs in without burning its session.
- **Sessions.** Only after the check is the state read: Playwright's
  storage state (the cookies of every site the context visited, local
  storage, IndexedDB) plus the application origin's `sessionStorage` under
  `session_storage` (put back by an init script, `browser.split_storage_state`),
  Fernet-encrypted. The start origin's `localStorage` is also read on the page
  itself (`ProfileBrowser.storage_state`, `browser.with_local_storage`):
  Playwright leaves out an origin it read empty or could not read, and an
  application keeping its session as a bearer token there, with no session
  cookie (XTM One's `copilot_token`), then reopened on its sign-in page. The
  page read fills only the keys Playwright's read lacks. The state is then
  probed in a fresh context (`probe_state`), as the next session loads it; a
  probe that fails after the application rendered signed in names what the
  state kept (`browser.persistence_note`: the cookies of the start host, the
  `localStorage` key names, the keys the page held and the state lost; names
  only, never a value), as does the "came back to the application" failure.
- **The username field.** Besides the structural selectors
  (`signin.USERNAME_SELECTORS`, now with `autocomplete~=email|username`), a
  password form whose username field no attribute names gets the text or
  email input placed before the password field in the same form. A password
  form that comes back twice without an error banner says which field the
  submit left empty ("email field left empty"), or that no username field was
  recognised (set `username_selector`).
- **The session's storage keys.** `login_config.keep_storage_keys`
  (a list; the tool's `login_selectors` and the dialog take it comma
  separated, `profiles.clean_login_config`, at most 30) names the localStorage
  and sessionStorage keys the application keeps its sign-in in
  (`ProfileSnapshot.keep_storage_keys`). A recording's page state reset
  (`browser.open_cleared`, `StorageReset`: an init script clearing the
  origin's stores at the next document's start, then disposed with the init
  script that puts the saved `sessionStorage` back, `ProfileBrowser.session_restore`)
  keeps them and never touches a cookie; see `dev-docs/video-studio.md`, "A
  clean page state per take".
- **Honest status.** `active` / `expired` are written by `profiles.record_probe`
  only, with `last_probe_at` and `last_probe_result` (`{authenticated, url,
  reason, checked_at}`); a session saved without a probe is `unverified`,
  and `effective_session_status` reads an `active` without a passing probe
  as `unverified`. Probes run after every save, on the keep-alive sweep
  (`keepalive.py`, `keepalive_minutes`), from
  `check_browser_profile_session` / `POST /browser-profiles/{id}/check`, and
  before every agent session and step 1 of every recording.

The challenge and keep-alive Redis keys keep their `video_studio:` prefix:
replicas of two versions share them during a rolling upgrade.

## Keep-alive (`keepalive.py`)

`keepalive_sweep` probes and refreshes at most one due profile per run
(`keepalive_minutes`, default `KEEPALIVE_MINUTES_DEFAULT` = 240;
migration `b7059renew01` moved the rows at the old default 0; 0 = never is a
person's choice; `KEEPALIVE_STATUSES`), under a Redis lease that keeps
one replica on it. It runs from the periodic hook of
`orchestration_adapters/browser_profiles.BrowserProfileSessionsAdapter`
(registered on every worker; no execution is ever created for it) at most
every `SWEEP_INTERVAL_SECONDS` per process, whatever feature is on.

## URL policy (`browser.py`)

Every navigation and every redirect hop goes through
`web_fetch.validate_outbound_url_async`, the agent browser tool's rule:
public addresses only (`browser.check_target`, `RedirectGuard` with the
profile's policy, a route filter for schemes and literal internal addresses).
An application on a private network needs the `browser_private_targets`
security option (`SECURITY_OPTIONS`, `SECURITY_CONTROLS`
`tools.browser_profiles_public_targets_only`; it was
`video_studio_private_targets`, whose stored value migration `v5965bprof01`
moves) AND the host declared on the profile (its start / sign-in URL hosts
plus `allowed_hosts` and its applications' hosts); loopback, link-local and
cloud metadata addresses stay refused.

## Applications a profile also signs in (`apps`)

An application that signs in through another one's identity provider opens
signed in with that application's saved session. The profile declares it in
`BrowserProfile.apps` (`[{name, url}]`, migration `b2549profapps01`, cleaned by
`profiles.clean_apps`: an http(s) URL without credentials, one entry per host,
at most `MAX_APPS`). Their hosts are hosts of the profile everywhere a host
counts: the URL policy (`snapshot()`), the hosts a session is saved for
(`agent_sessions.profile_hosts`) and the re-point rule (`destination`).
`profiles.covers_url` answers a lookup by URL (`list_browser_profiles` and
`GET /browser-profiles` take `url`). A page on a declared application is judged
as that application: `ProfileSnapshot.scoped_to(url)` / `as_app(app)` take its
URL as the start URL, no sign-in URL and none of the own application's marks
(`signed_in_selector`, `signed_in_url_pattern`, `signed_out_url_pattern`), so a
recording that starts there waits for it (`capture.record_take`) and
`check_browser_profile_session` probes each one once the profile is signed in
(`session_check.apps`). The profile still signs in at its own application. A
profile without `apps` reads as before.

`delete_browser_profile` deletes a profile from an agent with the REST delete's
rule (`can_manage`, audited); a shared profile, or one recordings were made
with (`VideoTake.profile_id`, set to null, the recordings kept), needs
`confirm=true`.

## Surfaces

- **REST** `/api/v1/browser-profiles` (`api/v1/browser_profiles.py`): list,
  create, read, update, delete, `sign-in`, `test-sign-in` (the dry run),
  `check`, `sign-out`, `GET /{id}/mfa-screenshot` (the open MFA prompt's
  screenshot, for whoever may use the profile) and `GET|POST /mfa/{challenge_id}`.
- **Agents**: `browser_profile_ids` / `browser_profile_read_only` on the agent
  API; `ga_browser_profile_ids` on `/auth/me` and `PUT /settings/preferences`.
- **Frontend**: `/browser-profiles` (`pages/BrowserProfiles.tsx`, Tools &
  Integrations, its hub card and command palette entries;
  `components/browser-profiles/`), the agent configuration card
  (`agent-detail/config/BrowserProfileSelectorCard.tsx`, shown with the
  `browser` tool), the General Assistant group of `My profile`
  (`Profile.tsx`, shown with the assistant's `browser` tool), and the
  Video Studio's `Browser profiles` button (`?tab=profiles` redirects).

## Tests

`test_browser_profile_agent_access.py` (the binding rules, the tool's profile
surface, read-only refusals, the session hooks, the compare-and-set),
`test_browser_profile_save_session.py` (`save_session`: the access checks,
the host scoping of the saved state, the probe that decides, the capture),
`test_browser_profiles_api.py` (the router outside the flag, binding and
preference checks, the copy, the keep-alive adapter),
`test_browser_profile_signin.py` (TOTP validation, the FIDO-bridge reader, the
status flags, the trace schema), `test_browser_profile_signin_chromium.py` and
`test_browser_profile_signin_entra.py` (the `chromium` CI job: the passkey /
FIDO flow end to end - bridge bypass, credential picker, otc, KMSI, the
redirect-chain mislabel), `test_browser_profile_apps.py` (the declared
applications, the lookup by URL, the delete tool), `test_browser_profile_totp.py` (the core TOTP
module) and `test_browser_profile_vault_credentials.py` (credentials read from
a vault: the write check, the resolved snapshot, failures named, the re-point
rule, the where-used scan).
