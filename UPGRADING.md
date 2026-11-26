# Upgrading

Operator-facing upgrade notes for self-hosted XTM One deployments. General
upgrade and rollback procedures live in the documentation site
([Deployment > Upgrade](docs/docs/deployment/upgrade.md)); this file documents
releases that need special attention.

## A user's email is changed by an administrator only

**No migration, no configuration change.**

- **Changed**: people can no longer change their own email from their profile: `PUT /api/v1/settings/preferences` answers `422` when it carries a new address. The other Filigran platforms find a person's account by this email, so an unproven address could take over someone else's account there.
- **New**: an administrator changes it from the user's page (`Email` card), or with `PUT /api/v1/users/{user_id}/email` and `{"email": ...}`, SSO accounts included. It works without a mail server; with one, the old and the new address are told of the change.
- **Reset links**: a password reset link stops working once the account's email is no longer the address it was sent to.

## The Filigran AI Gateway's throttle backs assignments off

**No migration, no configuration change.**

- **Back-off**: an assignment whose run meets the gateway's throttle (`502 provider_error`, `retryable: true`) or load shedding (`503 gateway_overloaded`) has its cap halved, as a provider's 429 already did.
- **Fewer requests**: a gateway chat call is sent at most 6 times (it was up to 18): the OpenAI SDK no longer retries under XTM One's own retries. Embeddings, images, speech, video and listings keep the SDK's retries.
- **Sent once**: an error marked `x-should-retry: false` (the gateway's spent quota or a circuit-open past 60 s, or OpenAI's own) fails at once on every OpenAI-compatible provider instead of after 5 retries; a rate limit still backs the assignment off.

## The Filigran AI Gateway provider is always offered

**No migration. Drop `FILIGRAN_GATEWAY_PROVIDER` from `ENABLED_FEATURES` if it is set.**

- **Changed**: the `FILIGRAN_GATEWAY_PROVIDER` feature flag is gone. Administrators can add a Filigran AI Gateway provider from **Settings → AI Models** on every deployment.
- **Leftover id**: `ENABLED_FEATURES=FILIGRAN_GATEWAY_PROVIDER` is logged as unknown and ignored.

## STIX scores before and after metric suite 16.0 are not comparable

**No migration. Rescore stored STIX baselines before comparing them with new runs.**

- **Changed**: `stix.entity_prf` and `stix.relationship_prf` now identify an object as OpenCTI's import does. An attack pattern without `x_mitre_id` takes the ATT&CK id of its last `mitre-*` external reference, and a location without `x_opencti_location_type` is a city, a country or a region by the property it carries. Two objects that were two identifiers (a technique with only its reference, the same technique with `x_mitre_id`) are now one, as OpenCTI merges them.
- **Effect**: a run scored before 16.0 and one scored after can differ on the same answers wherever a bundle omits those properties. Evaluation results keep the suite version they were scored with; a comparison across the boundary rescores the older run's stored answers with the current suite.
- **New, additive**: `stix.entity_prf.aligned`, `stix.entity_prf.soft`, `stix.relationship_prf.aligned` and, for a dataset whose references annotate a known set of types, `stix.entity_prf.aligned.closed` and `stix.relationship_prf.aligned.closed`. None is a default score of an evaluation.

## The chat WebSocket is removed: the web chat streams over HTTP

**No migration. Serve HTTP/2 to browsers at the reverse proxy, and move any script that opened the chat WebSocket.**

- **Removed**: `WS /api/v1/chat/conversations/{id}/ws`. The `/chat` page, the deck and video studio rail, voice mode and the Office add-in use the HTTP + SSE routes under `/api/v1/web/chat` ([Chat API](docs/docs/usage/chat-api.md)). A script sends with `POST /api/v1/web/chat/messages`, whose response streams the same frames as server-sent events, and follows the conversation with `GET /api/v1/web/chat/conversations/{id}/events`; Stop, Continue now, steering and approvals are `POST`s. There are no `ping` / `pong` frames and no close codes: a refusal is an HTTP status (`403` with `code` `chat_unavailable` or `terms_required`), a send refused before its request starts is an HTTP error with `detail` instead of an `error` frame, and a session that ends sends a `session_expired` frame before the stream ends.
- **HTTP/2 at the reverse proxy**: every open chat tab holds its own streams, so behind a proxy that speaks only HTTP/1.1 to browsers a few open tabs use up the browser's connections and block the app. Turn HTTP/2 on between browsers and the proxy, keep response buffering off for `/api/` and a read timeout above 15 seconds ([Exposing the platform](docs/docs/deployment/installation.md#exposing-the-platform)).

## The OpenSandbox images default to 0.6

**No migration. Action only if your OpenSandbox server cannot reach the new images.**

- **Images**: `OPENSANDBOX_IMAGES` now defaults to `sandbox-python:0.6` and `sandbox-nodejs:0.6` (was `0.5` and `0.4`). `sandbox-python` adds PyAV (a video frame decoded in the code interpreter) and keeps the AES zip stack (`pyzipper`, `pycryptodomex`, `cryptography`).
- **Before upgrading**: the OpenSandbox server must be able to pull or find `sandbox-python:0.6` and `sandbox-nodejs:0.6` (published as `xtmone/sandbox-python` and `xtmone/sandbox-nodejs`, or built with `backend/sandbox/build-image.sh`).
- **Keep the previous images**: set `OPENSANDBOX_IMAGES=python:sandbox-python:0.5,nodejs:sandbox-nodejs:0.4`; both stay published.
- **Kubernetes pools**: a pool's image is set on the pool, not by `OPENSANDBOX_IMAGES`; move it with the default.

## A Slack sender is recognised by their own member id

**One data migration (`s5811slackid01`), no action required.**

- **Before**: the Slack adapter identified a sender by the channel they wrote in. The first member of a channel whose email matched an account had that channel linked to their account, and any later member of the channel whose email did not match was answered as that account.
- **Now**: a sender is linked and looked up by their own Slack member id. A member whose email does not match an account is unidentified ("please register", or anonymous where the bot allows it).
- **Migration**: every Slack identity that names a channel (`slack:C...`, `slack:G...`, `slack:D...`) is removed from the users' linked channel identities. A member id an administrator linked (`slack:U...`, `slack:W...`) and the other channels' identities are kept. A member's own id is linked again on their next message whose email matches.
- **Rollback**: the downgrade restores nothing; the removed links never identified one person.

## The OpenSandbox images default to 0.4

**No migration. Action only if your OpenSandbox server cannot reach the new images.**

- **Images**: `OPENSANDBOX_IMAGES` now defaults to `sandbox-python:0.4` and `sandbox-nodejs:0.4` (was `0.3`). Same Python 3.14.8 and Node 24.22.1; Pillow's FreeType loads Debian's fixed Brotli instead of the bundled 1.2.0 (CVE-2026-111497).
- **Before upgrading**: the OpenSandbox server must be able to pull or find `sandbox-python:0.4` and `sandbox-nodejs:0.4` (published as `xtmone/sandbox-python` and `xtmone/sandbox-nodejs`, or built with `backend/sandbox/build-image.sh`); sandbox creation fails where it cannot.
- **Keep the previous images**: set `OPENSANDBOX_IMAGES=python:sandbox-python:0.3,nodejs:sandbox-nodejs:0.3`. `0.3` stays published as a rollback.
- **Kubernetes pools**: a pool's image is set on the pool, not by `OPENSANDBOX_IMAGES`; the Platform team moves it.

## Product sign-in without a consent page

**No migration. No action required for MCP clients.**

- **No consent page**: a product's sign-in client (`xtm-platform-<identifier>`, active platform registration, `openid` granted) is authorized at once when the person has an XTM One session, and right after the XTM One sign-in otherwise. Such authorizations are audited with `"consent": false`. An impersonation session is never signed in to a product.
- **Tokens**: the token response of a product sign-in has no `refresh_token`, and its `access_token` is bound to the client (`aud` = the client id): no MCP endpoint accepts it. A product must rely on the ID token only.

## Products sign their users in through platform registration

**No migration. No action required for MCP clients or for products that do not sign users in.**

- **Product sign-in**: a product gets its OpenID Connect client by sending `sign_in_redirect_uris` (its callbacks, on its own `platform_url` origin) with its platform registration, authenticated by `PLATFORM_REGISTRATION_TOKEN`; the answer carries its `sign_in_client_id`. Not with the published default token. Sending `[]` removes the client; unregistering or purging the platform removes it too, and a platform whose registration is not active signs nobody in.
- **Dynamic client registration** (`/mcp/oauth/register`) no longer gives OpenID Connect. A client registered that way since the OpenID Connect support that relied on `openid` gets no ID token any more: register the product as a platform instead.

## Secret vaults are connected, granted and picked in the UI

**One schema migration (`v5456vault01`, a new table), no action required.**

- **What it adds**: administrators connect HashiCorp Vault, AWS Secrets Manager, Google Cloud Secret Manager, Azure Key Vault and 1Password (a 1Password Connect server) in Settings > Security > Secret vaults, test them, grant them to everyone, groups or users with path prefixes, and disable or delete them, with no environment variable and no restart. Every secret field then offers **From a vault**: a picker that browses the vault, picks the key and checks it on the server. The OpenID Connect client secret, the Langfuse keys, an MCP server's OAuth client secret, the AI gateway token and a browser profile's username, password and TOTP secret now accept a reference too. See [Secret vaults](docs/docs/deployment/secret-vaults.md).
- **1Password**: the existing 1Password integration (agent tools) is unchanged and keeps its stored credentials; it now reads through the same Connect client as the new 1Password secret vault, and computes one-time password codes with the platform's one TOTP implementation. Connecting the vault is a separate, optional step for secrets the platform itself uses (secret fields, browser profiles); it may use the same Connect server with its own token.
- **Browser profiles**: existing credentials keep working unchanged. A username or password stored before this release that starts with `vault://` was typed into the site as is; it is now read from its vault when it names a connected one, and a sign-in that cannot read it stops saying so. A TOTP secret pointing into a password manager (`op://...`) is still refused, now with how to pick it from a vault instead. A browser profile holding a vault reference follows the re-point rule of every secret field: an edit moving its start URL, sign-in URL or private hosts is refused unless its editor may write that reference or the same edit replaces it.
- **Vaults declared in the environment** (`SECRET_VAULT_<NAME>_*`) keep working unchanged, with their `WRITERS` and `PATHS`, and are listed read-only in Settings. A declaration takes precedence over a vault of the same name connected in Settings. Every stored `vault://` reference keeps working.
- **What opens a vault connected in Settings** (a token, an AppRole secret ID, an access key, a service account key, a client secret) is stored encrypted with the platform's `SECRET_KEY`, like every other credential, and never returned by the API. Prefer a workload identity (IRSA, GKE Workload Identity, an Azure managed identity), which stores nothing. A vault connected in Settings never sends the platform's identity to an address a session chose: HashiCorp Vault's Kubernetes login (which posts the pod's service account token) and a service account token file are declared in the environment only, and an Azure vault's URL must be a key vault of an Azure cloud (`*.vault.azure.net` or a sovereign cloud's key vault domain). Nor is it reached on a loopback, link-local or cloud metadata address (private networks are): a HashiCorp or 1Password address and an AWS VPC endpoint are checked when saved, before each test and on each connection, a vault on the platform's own host (a Vault Agent, a 1Password Connect server beside it) needs its origin in the new `SECRET_VAULT_TRUSTED_ORIGINS` (environment only), and a Google service account key is exchanged at Google's token endpoint whatever its `token_uri` says.
- **A change to a vault applies on every replica and the worker within 5 seconds**, through the refresh loop that already carries the security options.
- **Code calling the vault write checks** (a fork, a plugin): `secret_fields_writer_problem()`, `reference_writer_problem()` and `retained_reference_writer_problem()` take a `principal` (`app.services.vault.store.vault_principal()`) instead of `is_admin`, and `require_vault_writer()` is now `async` and takes the session first.

## HubSpot activity tools need more private app scopes

**No migration. Action only to use the new HubSpot activity tools.** HubSpot integrations gain a record timeline, engagement reads and searches, forms and their submissions, sequences, teams, batch reads and an inbound engagement report. They read with the integration's existing private app token, which reaches only what its scopes allow.

- **Before**: the HubSpot tools stopped at CRM records and association IDs.
- **Now**: the new tools need the scopes listed on the integration's setup guide: `sales-email-read`, `crm.objects.emails.read`, `crm.objects.notes.read`, `crm.objects.calls.read`, `crm.objects.meetings.read`, `crm.objects.tasks.read`, `files.ui_hidden.read`, `forms`, `automation.sequences.read`, `crm.schemas.contacts.read`, `crm.schemas.companies.read`, `crm.schemas.deals.read`, `settings.users.teams.read` (and `crm.objects.owners.read`, already listed). A tool that meets a missing scope names the scope and itself; the existing tools keep working without them, and contact, company, deal and ticket reads show raw values where a label needs a missing schema scope.
- **Action**: a HubSpot Super Admin adds those scopes in HubSpot, `Settings` > `Integrations` > `Private Apps` > the app > `Scopes`, and commits the change. The access token stays the same: nothing changes in XTM One.

## A remote MCP server that redirects to another origin must be saved with its final URL

**No migration. Action only for a remote MCP server whose URL redirects elsewhere.**

- **Before**: the `mcp` library (now 1.30.0, was 1.28.1) followed any redirect of a Streamable HTTP or SSE server.
- **Now**: it follows a redirect only within the server's own origin (a trailing slash, `http` to `https` on the same host) and only when it keeps the request. A server whose URL redirects anywhere else (apex to `www.`, a vendor that moved its endpoint to another host, a `301` / `302` on a request that posts) stops connecting: its tools fail with a connection error until its URL changes.
- **Action**: run `Test Connection` on your remote MCP servers. One that fails with `Connection test failed: the server redirects to …` names where it points (without its query): if that is the intended server, save the server with that URL. The backend logs the same as a `WARNING`, and these failures count toward the connection circuit breaker like an unreachable server.

## Evaluation is behind the `EVALUATION` feature flag

**Action needed by a Copilot or dev deployment that uses evaluation: set `ENABLED_FEATURES=EVALUATION` (or add `EVALUATION` to the ids it lists) on the platform and the worker, then restart both.** From this release, evaluation (datasets, evaluations and their runs, and the persona rewrites they measure) is off unless the flag is set, and never available in XTM One mode.

- **Off**, its pages, API, scheduled runs and agent tools are gone. Nothing it stored is deleted: setting the flag brings everything back.
- **`ENABLED_FEATURES=*` turns it on too.** A deployment that must stay without evaluation lists its ids explicitly.
- **Set it on both.** A platform with the flag and a worker without it offers evaluation and fails every run with `Feature 'EVALUATION' is disabled on this platform.`

## Output schema bounds are no longer sent to Anthropic

**No action needed. This applies whether evaluation is on or off.** When an agent with an output schema answers on an Anthropic model, in a chat turn or an assignment run, the schema sent to the model now leaves out `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `minLength` and `maxLength`. Anthropic refuses these keywords with a `400`; the other providers are sent the schema as before.

- **The model is not held to those bounds any more.** The answer is still checked against the full schema once it arrives: a value outside a bound is logged as a warning and the answer is delivered, as for any answer that does not match its schema.
- **An agent that relies on a bound**, a score from 0 to 100 for instance, can state it in its instructions.

## The evaluation schema lands as one migration

**No action needed, except for an evaluation saved on a replica still on the previous release during the rollout.** Evaluation's schema changes land as one migration, `e1780evp001`. While a replica still on the previous release serves, in Copilot or dev mode:

- **An evaluation saved there** keeps its resume secret in clear, which its callbacks no longer read, and its tolerance in `speed_tolerance` only. Set both again on that evaluation once the rollout ends.
- **Appending a dataset entry, or linking a dataset to an evaluation, there** can fail with a `500` until the replica is replaced: positions are unique from this release, and the previous code could append at a taken one.

`speed_tolerance` is kept for that window, with the values copied into `performance_tolerance`; a later release removes it.

## Existing evaluations and datasets need a family before they run

**Action needed by a Copilot or dev deployment that used evaluation before this release: once evaluation is enabled (see *Evaluation is behind the `EVALUATION` feature flag* above), set a family on each evaluation you run, and on each dataset linked to one.** This release scores an evaluation by its output family, and every launch requires one. The migration `e1780evp001` adds the `family` column to evaluations and datasets but leaves it empty, and nothing infers it: a wrong guess would score an evaluation under the wrong metrics without anyone noticing.

Until the family is set, the evaluation runs nothing:

- **Run, replay, re-run and re-score** are refused with `This evaluation has no output family set. Select an evaluation family before running it.`, or, for a linked dataset without one, `The following linked dataset(s) have no family set and must be fixed before this evaluation can run:` followed by each dataset's name and id.
- **A scheduled evaluation** creates no run. The worker logs a `WARNING` once per cause, and the evaluation runs at the first tick after its family is set.
- **Approving a persona rewrite** measured by such an evaluation is refused with the same message.

Where to set it:

- **An evaluation:** its page, **Configuration** tab.
- **A dataset:** its page, **Settings** tab, **Output Family** card. The Datasets list marks each dataset without one **Needs family**. Give each dataset the family of the evaluations it is linked to. A different one is reported as a conflict when you save it but is not refused, and a run then scores the dataset under the evaluation's family.

## Evaluation runs in progress during the upgrade are failed

**No action needed. Re-run what you still need.** A run the previous release left in progress is not continued by this one. Once no execution of its evaluation is still pending or running, and the stranded-run grace period has passed since it started, it is failed with its unfinished entries, with the error `Interrupted by an upgrade: no execution was left to continue this run — re-run to retry`.

- **During a rolling deploy**, a run whose evaluation a replica still on the previous release is running is left alone until that execution ends, even when the evaluation is run again meanwhile.
- **An evaluation run again on this release** fails such a run as soon as its new execution starts and no other is running, with `Abandoned by a worker that did not survive its attempt — re-run to retry` instead.
- **Nothing recorded is lost**: the entries it already scored keep their results.
- **A run waiting on callbacks** is not failed this way: its entries time out at their callback deadline, as before.
## Docker Compose passes the on/off switches set in .env

**No migration. Action only if your `.env` sets a switch you do not want applied.**

- **Before**: `docker-compose.yml` passed the platform and worker containers a fixed list of variables, so most on/off switches `.env.sample` documents (`DISABLE_CUSTOM_TOOLS`, `ASSIGNMENT_QUEUE_ENABLED`, `MCP_DCR_ENABLED`, `PROMETHEUS_ENABLED`, ...) did nothing when set in `.env`.
- **Now**: it passes every one of them except `DEBUG`, `A2A_ALLOW_INSECURE_LOOPBACK` and `TRUST_PROXY_HEADERS`. A switch your `.env` sets takes effect at the next `docker compose up -d`, and one that is also in Settings > Security is then locked there to that value.
- **Before upgrading**: read the switches set in your `.env` (`grep -E '^[A-Z_]+=(true|false)' .env`) and remove those you do not want applied.
- **Unset switches**: unchanged; the containers keep the platform's defaults, and a blank value for an on/off switch now counts as unset instead of stopping startup.
- **Kubernetes and other deployments**: unchanged.

## The OpenSandbox images default to 0.3

**No migration. Action only if your OpenSandbox server cannot reach the new images.**

- **Images**: `OPENSANDBOX_IMAGES` now defaults to `sandbox-python:0.3` and `sandbox-nodejs:0.3` (was `0.1`). Custom tools and code-interpreter runs get Python 3.14.8 on a distroless Debian 13 image and Node 24.22.1 (was Node 22), with the CA bundle that blocks the compromised eMudhra intermediates.
- **Before upgrading**: the OpenSandbox server must be able to pull or find `sandbox-python:0.3` and `sandbox-nodejs:0.3` (published as `xtmone/sandbox-python` and `xtmone/sandbox-nodejs`, or built with `backend/sandbox/build-image.sh`); sandbox creation fails where it cannot.
- **Keep the previous images**: set `OPENSANDBOX_IMAGES=python:sandbox-python:0.1,nodejs:sandbox-nodejs:0.1`. `0.2` stays published too, as a rollback.
- **Kubernetes pools**: a pool's image is set on the pool, not by `OPENSANDBOX_IMAGES`; the Platform team moves it.

## One account per email address, whatever its case

**One data migration (`u6299emlcase01`), not reversible, no action required.**

- **Before**: an SSO sign-in matched and stored the IdP's email case-sensitively, so a person with `jane@x` could get a second account `Jane@x`, and invitations for that address failed with a 500.
- **Merge**: each group of accounts whose emails differ only by case is folded into the lowercase one (the oldest when none is lowercase). It keeps its id, password, role and profile, takes the duplicate's SSO link if it has none, and keeps its own active or deactivated state (a duplicate disabled by an administrator does not disable it). What the duplicate owned (conversations, keys, memberships, sharing lists) moves to it; a row it already has an equivalent of is dropped. Quota counters and daily usage rollups add up on it (counts, tokens, cost) and a merged day's latency histograms add up; its percentiles are those of the busier account. Only the stored counters move: the quota counters of the current period live in Redis and are not carried over, so the survivor starts the period with its own count. An administrator who got the admin role only through the duplicate (auto-admin on SSO) loses it until their next SSO sign-in applies it again.
- **Duration**: the upgrade rewrites every row that names a duplicate, its traces included, in one transaction, so startup takes longer the more it produced. Running the migration on its own before the rollout (`python -m alembic -c alembic/alembic.ini upgrade head`, from the backend directory of the new image) keeps that out of the app's boot.
- **Sessions**: a session or token issued for a deleted duplicate stops working; the person signs in again.
- **Emails**: every remaining email is lowercased, `ADMIN_EMAIL` included, and a unique index on `lower(email)` keeps it that way. Channel (Slack, Teams, ...) and profile lookups ignore case.
- **Rolling deploy**: a pod still on the previous version that creates a case-variant account after the index exists fails that request. The upgrade merges one created while it runs.
- **Rollback**: the downgrade drops the index; merged accounts are not restored.

## STIX objects get pycti's standard ids in the container images (issue #5744)

**No migration, no action required.**

- **Images**: the platform and worker images install `libmagic1t64`, which pycti needs as soon as it is imported, and their build fails without it. Before, pycti could not load in any image: the STIX bundle validator gave the objects it repaired an id of its own, which OpenCTI does not recognise as an object it already holds, and logged nothing. A self-built image needs `libmagic` too.
- **Ids**: an id the validator gives an object is now pycti's standard id. An object repaired before keeps the id it got, since the validator never rewrites a well-formed id, so the same object repaired before and after the upgrade can carry two different ids, and OpenCTI can hold it twice.
- **Logs**: when pycti cannot give an object its standard id, a `WARNING` says why, once an hour per cause.

## Every default assistant writes reports, builds decks and makes videos

**One data migration (`r5931gareport`), no action required.**

- **Built-in assistants**: the `CTEM Assistant`, the `XTM-One Assistant`, the `OpenCTI Assistant`, the `OpenAEV Assistant`, the `OpenCRQ Assistant` and the `FIMI Analyst` now carry report generation, presentation generation, template search, the Video Studio and file generation. Built-in agents are rewritten from code on every boot. The product assistants are what the embedded chat of OpenCTI, OpenAEV and OpenCRQ reaches, so a deck, a report or a video can now be asked for from inside a product.
- **Skills**: the new built-in `Report Design` skill, with `Presentation Design` and `Video Production`, is bound to those assistants and carried by a General Assistant whose skills were never chosen. On XTM One the General Assistant answers with the `CTEM Assistant`'s skills, which now include all three.
- **General Assistant tools**: a new General Assistant starts with report generation (`generate_report`). The migration adds it once to the General Assistant that exists at upgrade; an administrator who removes it afterwards keeps that choice. A user's own General Assistant tools (`My Profile`) are not changed.

## An evaluation's `quality_weight` is no longer served

**Action needed only by an API caller that reads it: no evaluation response or export carries `quality_weight` any more.** It weighed quality against speed in the overall score that was retired, and nothing has scored with it since. A create or update may still send it, as the evaluation pages of earlier versions do on every save: it is accepted and ignored, and the API schema marks it deprecated. An evaluation package that names it still imports, the value ignored. The column keeps its stored values. No migration.

## Evaluation resume secrets are encrypted

**No action needed.** The migration `e1780evp001` encrypts each evaluation's callback resume secret (`trigger_config.resume_secret`) into `resume_secret_enc` and removes the clear copy; a number JSON cannot hold, which only a manual SQL edit can store, is written as `null`. The secret itself does not change, so the external systems that sign `POST /evaluations/resume` keep working.

- **Not readable any more**: an evaluation response serves the secret as `"***"`, and an evaluation export leaves it out. An update that omits it, or sends `"***"` back, keeps it; `null` or `""` removes it.
- **Dropped**: a stored secret no sender could sign with (a boolean, a list, an object) or one holding a vault reference is removed, and the migration logs the evaluation's id. Such a secret already failed every callback; set a new one through the API.
- **Callback mode needs a secret**: an evaluation with `await_callback` on and no secret is refused with a `400` on trigger, replay and re-run, instead of parking every entry until its callback timeout. An imported evaluation brings no secret: set one before running it in callback mode.
- **Rolling back**: the migration's downgrade puts each secret back in clear as `resume_secret`.

## `POST /api/v1/evaluations/{id}/import-hf` is removed

**Action needed only by an API caller of that route.** It appended Hugging Face dataset rows to an evaluation and had no caller in the UI. Import a Hugging Face dataset with `POST /api/v1/datasets/hf-import`, then link it to the evaluation with `POST /api/v1/datasets/{dataset_id}/link/{evaluation_id}` (or with Link Dataset in the evaluation's Dataset tab) instead. No migration.

## OpenAEV tools act on one tenant of a multi-tenant OpenAEV

**No migration. No configuration change is required**: left empty, the new optional **Tenant ID** field of the OpenAEV integration keeps the tenant chosen automatically. A single-tenant OpenAEV is unaffected.

- **Creates work**: scenario, inject, simulation and other creates no longer fail with `TENANT_WRITE_SCOPE` on a multi-tenant OpenAEV. Every OpenAEV tool request on a tenant-owned route goes to one tenant (`/api/tenants/{id}/...`); identity (`/api/me`), settings, platform-level and autonomous-run routes stay unchanged.
- **Which tenant**: the integration's **Tenant ID** field when it is set. Left empty, the tenant is chosen the way OpenAEV does for a request that names none: the account's only tenant, or the default tenant (`default_tenant_id` of `GET /api/settings`) when the account belongs to several, it among them. An account that belongs to several tenants, none of them the default, needs the field: until then the tools stay on the unscoped routes and a `TENANT_WRITE_SCOPE` error says so.
- **Reads narrow**: a token that spans several tenants now reads only the chosen tenant's rows through the OpenAEV tools: lists, and reads by ID too (an ID from another tenant answers 404).
- **Lookups**: choosing the tenant reads `GET /api/settings` (read access to platform settings) and `GET /api/me/tenants`. When the settings cannot be read, the tools keep working on the unscoped routes and a `TENANT_WRITE_SCOPE` error says the lookup failed; the Tenant ID field needs neither lookup. A `TENANT_ACCESS_DENIED` error names the tenant the request went to.
- **Where to set it**: on an OpenAEV registered with XTM One (`xtm_one` mode), the integration page stays read-only except the **Tenant ID** field, which an administrator edits; a new registration keeps it (a registration still rewrites every other key of the integration's configuration). On an API-key integration (`copilot` / `dev` modes), it is on the form. The lock on a registered integration's other keys holds for the REST API and the agent builder's `update_integration` tool alike.
- **API**: on a registration-only integration, `PUT /api/v1/integrations/{id}` changes only the keys the catalog lists in `registration_editable_extra_fields` (served in the provider catalog); another key sent with a new value answers `400`. The agent builder's `update_integration` tool applies the same rule: it merges those keys into the stored configuration and refuses any other change without saving.

## Remote MCP server URLs are held to a destination policy

**No migration. No action required by default, except for a server saved at a cloud metadata address (below).**

- **New security options**: `MCP_SERVER_URL_POLICY` (`MCP server URL policy` in Settings > Security > Security options) and `MCP_SERVER_TRUSTED_ORIGINS` (`Trusted MCP server origins`). `docker-compose.yml` forwards both to the platform and the worker; left blank they leave the field in Settings > Security in charge. The default, `permissive`, refuses only cloud metadata addresses (`169.254.169.254` and the other instance-metadata endpoints), so every MCP server that worked before keeps working, one on `localhost` or a private network included. `private_networks` also refuses loopback, link-local, unspecified, reserved and multicast addresses; `public_only` refuses every address that is not globally routable (reach an on-prem server through a relay group). A trusted origin is reached at any address but a metadata one under both stricter modes.
- **Where it applies**: when a remote (`sse` / `streamable_http`) server's URL is saved - the form, `PUT` (when the URL, transport or relay group changes), bundle import, configuration import, agent import, the agent builder's `create_mcp_server` / `update_mcp_server` - and on the address every connection to it opens: the connectivity and tool tests, tool calls, the authorization probe, and OAuth discovery and dynamic registration. Each redirect hop is checked again. A server routed through a relay group is judged only on what its URL spells.
- **A server already saved at a cloud metadata address** (or at a name resolving to one) stops connecting. No MCP server is served there; point it at its real address.
- **Outbound proxies**: under the default `permissive` policy, a remote MCP server the environment sends through a proxy (`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`, `NO_PROXY` applied) still goes through it, after the platform refuses a URL that is, or resolves from the platform to, a metadata address. The two stricter modes never use the proxy: they connect directly, because the address connected to is what they judge, and say so once per process in a `WARNING`. A deployment that reaches MCP servers only through a proxy and turns on a stricter mode routes those servers through a relay group. `.netrc` is not read for these connections. `SSL_CERT_FILE` / `SSL_CERT_DIR` are still honoured.
- **Saving a server kept from before a stricter mode**: an update that leaves its URL, transport and relay group unchanged (renaming it, re-tagging it, changing its sharing, from the edit form included) is saved; one that changes where it connects is judged. Its connections stay refused by the policy.
- **Validation**: `transport` must be `stdio`, `sse` or `streamable_http`, `auth_mode` `headers` or `oauth`, and a remote URL an `http` or `https` URL with a host and a valid port; anything else is a `422` instead of a server that fails when first used. A server already stored with another value is not rewritten.
- **Security audit**: two new controls, `mcp.server_url_policy` (enforced under `private_networks` or `public_only`) and `mcp.server_trusted_origins` (enforced while empty).

## Test connection of an imported integration bundle ignores the proxy and CA environment

**No migration, no configuration change.**

- **Environment**: `Test connection` on an integration imported from a `filigran-integration-v1` bundle no longer reads `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` or `SSL_CERT_FILE` / `SSL_CERT_DIR` from the platform's environment. A bundle endpoint reached only through such a proxy, or trusted only through such a CA bundle, now answers `<name> connection failed`.
- **Tools**: the bundle's tool calls get the same rule. A release that has the `Test connection` change without this one still sends those tool calls through the environment, so for such an integration the test fails while its tools work.
- **Redirects**: an endpoint that answers with a redirect is reported as such (`answered with a redirect (HTTP 302), which is not followed`): check the integration's base URL. The redirect was not followed before either.
- **To reach an endpoint inside a network** the platform cannot reach directly, route the integration through a relay group ([Relays](docs/docs/usage/resources/relays.md)) instead of an environment proxy.

## The CTEM Assistant creates agents itself, and an agent's traces stay with the people who may read its chats

**No migration.** Built-in agents are rewritten from code on every boot. **Review your agent shares** if a `Can configure` grantee needs an agent's traces: that tier no longer reads them (third point).

- **XTM One**: the `CTEM Assistant`, every user's default chat agent, now carries the platform builder (`agent_builder`, the `XTM One` suite of the chat tool picker) next to its other tools, so "create an agent that ..." is answered there instead of being transferred to the `XTM-One Assistant`. It acts as the chatting user, through the same permission checks as the REST API; `AGENT_TOOLS_STRICT_ADMIN_ACCESS` applies as it does to the General Assistant. The Security audit counts it among the agents carrying the agent builder.
- **Trace inspection follows the rule for an agent's chats**: an agent's traces hold its users' conversations, inputs and outputs included, so only an admin, the agent's owner or a `config_with_chats` grantee (`Full access (incl. chats)`, directly or through a group) reads them, as its `Chats` tab already was. That is the traces API, the agent's `Observability` tab, and trace inspection inside a conversation beyond what the agent carries itself: what the platform builder brings along, and what a conversation's tool picker or a flow's Tool node adds to the agent. Other users chatting with the `CTEM Assistant` or the `XTM-One Assistant` get no trace inspection. An agent configured with `Inspect My Traces` or `Inspect Agent Traces` explicitly, and assignment runs, are unchanged.
- **`config` grantees lose trace access**: someone an agent is shared with as `Can configure` still configures it, but the traces API answers `403` and the agent's `Observability` tab is hidden for them; its `Feedback` tab stays. Share the agent as `Full access (incl. chats)` with whoever must keep reading its traces.
- **Flows check who may read an agent's traces**: saving a flow (a save, a split, a version restore, or the flow builder tool) that adds `Inspect my traces` or `Inspect agent traces` to an agent its writer may not read the traces of is refused with a `422` naming the node (`trace_tools.not_readable`), unless the agent carries the suite itself. A flow saved before runs as it did, except that the trace tools are only wired when the person running it may read them.
- **A2A partners get no trace inspection and no platform-builder writes**: an A2A task runs as its exposure's run-as identity (for a built-in agent, the account it was seeded with, usually an administrator), but the partner is an outside caller, like a channel sender without an account. The task, and every consult, transfer or background delegation it makes, gets no trace inspection and only the read half of the platform builder, acting as a caller without an account (company-managed objects); the flow builder and the evaluation tools are not wired for it. A channel sender without an account gets the same platform-builder surface.

## XTM One Relay, on by default

**One migration (`r5140relay01`), no action required: nothing is routed through a relay until someone chooses to.**

- **Migration**: creates `relay_groups`, `relays`, `relay_enrollment_keys` and `relay_request_logs`, and adds a nullable `relay_group_id` (foreign key `ON DELETE RESTRICT`, added `NOT VALID` then validated, partial index built concurrently) to `integrations` and `mcp_servers`. Nothing is rewritten or backfilled; every existing integration and MCP server keeps calling directly.
- **On by default**: the `Relays` page appears under `Tools & Integrations`, and integrations, remote MCP servers and flow HTTP steps get a `Route through` choice (default `Direct`). Only administrators create relay groups. `RELAYS_ENABLED=false` removes the whole subsystem: every relay endpoint returns 404 and the page and the choice are hidden. The `RELAYS` feature flag is gone; a leftover `ENABLED_FEATURES=RELAYS` is logged as unknown and ignored.
- **Distribution**: the relay installers and archives are embedded in the platform image and served from `/api/v1/relays/dist/...`; nothing external is needed, air-gapped deployments included. See [Relays](docs/docs/usage/resources/relays.md) and [Configuration > Relays](docs/docs/deployment/configuration.md#relays).

## The General Assistant creates agents out of the box and follows its linked agent

**One data migration (`g5209gadefault`), no action required.**

- **Default tools**: a new General Assistant starts with the platform builder (`agent_builder`, shown under the platform's name) and web fetch, next to file generation, presentations and template search. The migration adds those two tools once to the General Assistant that exists at upgrade, to the list as saved when it runs (a save made during a rolling upgrade is kept). An administrator who removes them afterwards keeps that choice. A user's own General Assistant tools (`My Profile`) are not changed. The platform builder acts as the chatting user, through the same permission checks as the REST API. While `AGENT_TOOLS_STRICT_ADMIN_ACCESS` is off (the default), that includes an admin's reach over colleagues' private agents, the reach the Security audit already reports for the General Assistant (`access.admin_reach_private_assignments`). Turn it on to keep an admin's General Assistant to the admin's own, shared and company-managed objects.
- **Link to Agent** (Settings > General Assistant) now does what it says: a new conversation started without an agent opens the linked agent, the chat page selects it by default, and the General Assistant's channel messages are answered by it, on the channels the General Assistant's `Channels & Routing` settings allow (that card is now shown while an agent is linked). Only an enabled company-managed agent every user can chat with can be linked. A link that does not meet that rule (saved before this release, or an agent since made private, restricted or disabled) is not followed: the General Assistant answers as before, and the settings screen says why. In XTM One mode the General Assistant is linked to the CTEM Assistant, so General Assistant messages on a channel are now answered by the CTEM Assistant. Conversations started with the General Assistant before the upgrade keep it. A web conversation started while the link is in effect stays the linked agent's, like one started by picking that agent; channel conversations follow the link message by message.


## Scoped personal API keys, with expiry and per-key limits

**One migration (`k5135apikeys`), no action required: every existing key keeps working exactly as before.**

- **Migration**: adds nullable columns (and `read_only`, `NOT NULL DEFAULT false`) to `user_api_keys` in one lock-safe `ALTER TABLE`; nothing is rewritten or backfilled. A key that exists at upgrade has no scope set, which means every surface, read-write, every agent, no expiry and no limit of its own - its access before the upgrade.
- **New keys** can be limited to some surfaces (REST API, LLM proxy, MCP endpoints), made read-only, restricted to selected agents, given an expiry and their own rate limit and quota. A request outside the key's scopes is refused with `403`, an expired key with `401` everywhere, a key over its own limit with `429` (the owner's quotas still apply, and the owner's other keys keep working). A key created with a name only keeps the full access keys always had.
- **Maximum lifetime**: `API_KEY_MAX_LIFETIME_DAYS` (also `API key maximum lifetime (days)` in Settings > Security > Security options), `0` (no maximum) by default. Setting it bounds keys created or re-dated from then on; it never cuts off an existing key. The Security audit lists the keys that outlive it (`authentication.api_keys_beyond_max_lifetime`), and while no maximum is set the ones that never expire (`authentication.api_keys_without_expiry`, `Info`) and those of full scope (`authentication.api_keys_full_scope`, `Info`). Neither `Info` finding raises a notification.
- **Usage tracking**: a key's `last used` and request count now cover every surface. Before, a key used only on the REST API showed `Never used`.
- **Impersonation**: an administrator impersonating a user can no longer change or delete that user's API keys (`403`), as creating one already was refused; the Profile disables those buttons and says why.
- **MCP one-click packages**: the key a package download creates is scoped to the MCP endpoints and, with a maximum lifetime set, expires within it.
- **Administrators** list every key with its owner, scopes, expiry and last use, and revoke one, in `Settings > Security > API keys` (each finding above links there, filtered on its keys) and on a user's `API Keys` tab, or through `GET /api/v1/admin/api-keys` and `POST /api/v1/admin/api-keys/{id}/revoke`. Creating, changing, deleting and revoking a key are recorded in the audit log, and the owner of a revoked key is notified.

## Tool approvals are opt-in everywhere, and each setting has one home

**No action required, and no stored setting changes.** The migration
(`o1ptin0173`) only turns the column default of
`platform_settings.approve_tool_calls_default` to `false`, so a fresh install no
longer starts with every agent asking. An install that turned "every agent asks"
on, or that an earlier release created with it on, keeps its value; no agent row
is written.

- **Where things moved.** Tools > Approvals is gone. The platform's side is in
  Settings > Security > Tool approvals: whether approvals are in use and which
  agents ask, the `Every agent asks by default` switch, `Tool rules` (the former
  whitelist), the connected platforms' policies and `Suspend all approvals` (the
  former platform switch). A person's own approvals are in Profile > Tool
  approvals. An agent's own setting is `Ask before running tools` under
  Configuration > Tooling. Old `/tools?tab=approvals` and `/tools?gate=<tool>`
  links redirect.
- **Nothing is shown while nothing asks.** The approval badges, columns, cards and
  agent icons appear only while an enabled agent (or the General Assistant) asks
  before running tools and approvals are not suspended.
- **API.** `GET /api/v1/tool-approvals` adds `approvals_in_use`; the new
  admin-only `GET /api/v1/tool-approvals/usage` lists the agents that ask.

## Configurable session lifetime and idle timeout

**No action required, no migration, and nobody is signed out by the upgrade.** The defaults are today's behaviour: a session lasts 7 days from its sign-in, with no idle timeout.

- **New settings** in Settings > Security > Security options (section Authentication), each also an environment variable that wins and locks the field: **Absolute session lifetime (minutes)** (`JWT_EXPIRE_MINUTES`, default `10080`, now a supported setting) and **Idle session timeout (minutes)** (`SESSION_IDLE_TIMEOUT_MINUTES`, default `0`, off; otherwise 5 minutes at least). A change applies without a restart and is recorded in the audit log. See [Session lifetime and idle timeout](docs/docs/deployment/authentication.md#session-lifetime-and-idle-timeout).
- **Sessions signed in before the upgrade keep working**: they carry no sign-in time claim, so their lifetime counts from when they were issued, and they still expire when they always would have. Once an idle timeout is turned on, they are idle-tracked like new sessions, starting from their next request.
- **Lowering the lifetime ends the sessions already older than the new value** on their next request; raising it applies from the next sign-in. A password change, an impersonation and the XTM One Desktop browser sign-in no longer start a fresh lifetime: they carry the session's sign-in time on. An impersonation ends with the administrator's own session.
- **The idle timeout counts only what the person does** (clicks, keys, the wheel or a touch, coming back to the tab), never what the web app requests on its own. It needs Redis, and fails open: while Redis cannot be reached, sessions are not ended for inactivity (the lifetime still applies), and the Security Audit says so.
- **An ended session** is answered `401` with `detail.code` `session_expired` (`reason`: `lifetime` or `idle`) instead of `Invalid token`, the chat WebSocket is closed with code `4005`, and the activity stream ends with a `session_expired` event. A script that drives the web app's own session token reads the same `401` as before; API keys (`fcp-`), MCP keys and tokens, and the tokens of OpenCTI, OpenAEV and OpenCRQ are not affected.
- **Security Audit**: `authentication.session_lifetime_long` now names the field and no longer calls `JWT_EXPIRE_MINUTES` unsupported, with a pass (`authentication.session_lifetime_bounded`) at a day or less; new findings report the idle timeout (`authentication.session_idle_timeout_off` / `_long` / `_on`, and `infrastructure.session_idle_timeout_fails_open` / `_unenforced`), with a new control, `authentication.session_idle_timeout`.

## Secrets can be read from an enterprise vault, opt-in

**No migration, and nothing changes on upgrade.** Until an operator declares a
provider (`SECRET_VAULT_<NAME>_TYPE`), every secret field keeps the value it
holds and works as before, unless that value itself starts with `vault://`
(see below).

- **What it adds**: the secret fields of integrations, MCP servers, LLM providers, variables and channel bots accept a `vault://<provider>/<path>#<key>` reference, read from HashiCorp Vault, AWS Secrets Manager, Google Cloud Secret Manager or Azure Key Vault at use time and kept in memory for `SECRET_VAULT_CACHE_SECONDS` (300 by default). See [Secret vaults](docs/docs/deployment/secret-vaults.md).
- **Who may write one**: administrators only, unless a provider sets `SECRET_VAULT_<NAME>_WRITERS=all`. A reference makes the platform send the secret wherever the entity points, so leave the default unless every user may read every secret the provider's credentials reach, or narrow it with `SECRET_VAULT_<NAME>_PATHS`.
- **Declare the provider on the platform and the worker**: the worker runs the scheduled work that reads the secrets. A provider declared on one only fails on the other with `provider_not_configured`.
- **A stored secret that itself starts with `vault://`**: the prefix is now reserved, so such a value (in one of the secret fields above) is read as a vault reference whether or not a provider is declared, and the feature using it fails with the generic vault error instead of sending the literal. Before or after upgrading, open Settings > Security: the Security Audit lists every such value, naming the entity and the field (never the value), under `encryption.vault_reference_unknown_provider` when it names an undeclared provider and under `encryption.vault_reference_invalid` when it is not a valid reference. To keep a literal that must start with `vault://`, store it in a vault and reference it. Every other secret field (the SSO client secret, an OAuth default, the Langfuse keys, an AI adoption key, ...) refuses a `vault://` value when it is saved (`422`); one already stored there makes the feature reading it fail, and a stored OIDC client id or secret turns OIDC sign-in off (logged as a warning) while local login keeps working.
- **Code reading a secret column directly** (a fork, a plugin): `decrypt_value()` now raises `SecretReferenceError` on a column holding a reference instead of returning the reference as if it were the secret. Read a secret through `reveal_field()` / `reveal_json_field()` / `reveal_ciphertext()` from `app.services.vault`, which resolve references, or `decrypt_stored_value()` from `app.core.security` to keep, compare or export the stored text as it is. `tests/test_vault_secret_readers.py` fails on a new direct reader.

## Evaluations run in Full Loop by default, and a failed Full Loop entry fails

**Action needed only by an API or import caller that relies on LLM Only without saying so: pass `"execution_mode": "llm_only"`.** Existing evaluations keep their mode. The migration `e1780evp001` adds the nullable `evaluation_runs.execution_mode` column.

- **Default**: an evaluation created through the API or imported without `execution_mode` now runs in Full Loop (the agent as it is configured: handovers, tools, knowledge bases), as one created in the UI already did. The built-in OCR benchmark declares LLM Only and keeps it. An ownerless Full Loop evaluation is listed by the Security Audit's `access.ownerless_evaluations_restricted_reach`, so new ones may appear there.
- **Unknown mode**: an `execution_mode` other than `llm_only` or `full_loop` is refused with a 422 naming the two values, on create, edit and import; it used to be stored and run as LLM Only. An evaluation already storing such a value still runs as LLM Only and its settings say so; a copy of it is written `llm_only`.
- **No fallback**: a Full Loop entry whose agent run fails is recorded with its error and scored 0, like any other failure. It used to be answered again by the persona alone and scored as the agent's answer. Answers given that way before the upgrade are never reused, and are marked "Fell back to LLM Only" in their run.
- **Labels**: a run that ran in LLM Only is labeled on the evaluation, run and comparison pages, and the evaluation agent tools report each run's `execution_mode` and `fallback_entries`.

To roll back, pass the mode explicitly; the migration's downgrade drops `evaluation_runs.execution_mode`.

## Assignment dispatch follow-ups: scoped 429 back-off, mail threads kept in order

**No migration, no configuration change.**

- **429 back-off**: only the assignment whose run met a provider's HTTP 429 has its cap halved, for a fixed window. A 429 met in a chat, an evaluation or an embedding no longer halves every assignment's cap.
- **Mail threads**: an email trigger now runs the deliveries of one thread one at a time, in arrival order (Microsoft `conversation_id`, Gmail `thread_id`); other threads still run side by side. An explicit `ordering_key` or `max_concurrent_runs` wins, `"ordering_key": ""` turns it off, and `ASSIGNMENT_MAX_CONCURRENT_RUNS=1` restores one run at a time everywhere.
- **Runs that are not deliveries**: while a manual, follow-up, scheduled or chained run is in flight, the deliveries of its assignment wait for it, as before.
- **Metrics**: the `xtmone_assignment_*` gauges lose their `assignment` label (the assignment's name) (a private assignment's name must not leave its owner's scope). A dashboard or alert grouping by that label must group by `assignment_id`.

## Per-tool approval for tool calls, opt-in

**No action required, and nothing changes on upgrade.** No agent starts
pausing: the upgrade switches approval on for none of them, existing or
created later, and rewrites no agent row. A fresh install starts with it on.

With approval on, a tool call pauses for a person unless the tool is on the
**HITL whitelist** (Tools → Approvals).
The whitelist ships with read-only tools; tools that write, send or reach
outside the platform, and MCP tools, are not on it.

- **Turn it on per agent** (agent settings → Tool approvals), including on
  built-in agents, where an administrator may set it and the change is audited.
- **Or install-wide** under Settings → Security → Tool approvals: agents that
  set nothing of their own follow that default, off on an upgraded install.
  The Security audit lists it as information (`agents.tool_approvals_default_off`),
  never as a warning.

Nothing gates until an agent (or the install default) opts in. Until then an
upgrade adds no Security audit warning, no audit log entry for a chat turn and
no new notification: a paused human-in-the-loop run is notified exactly as
before.
- `human_in_the_loop` keeps its meaning: the prompt-driven review and the
  `request_human_input` tool. It does not turn the per-tool gate on.

Before turning it on for agents that run unattended, note that a paused run
waits for an answer with no timeout, and its owner is notified. Clients that
cannot show a prompt get an explanatory message instead; see
[Chat approvals in client applications](docs/docs/deployment/chat-approvals-in-clients.md).
A connected Filigran platform's read tools are on the whitelist; whether its
other tools ask is set per platform (`TOOL_APPROVALS_PLATFORM_DEFAULT` seeds it at
first registration).

## Queued assignment deliveries start in parallel, and polled events always queue

**No migration. Delivery-triggered assignments now run up to 10 runs at once; `ASSIGNMENT_MAX_CONCURRENT_RUNS=1` restores one at a time.**

- **Before**: an assignment ran one execution at a time. A polled email, Teams or Slack message reaching a busy assignment stayed with its provider until a poll (once a minute) found the assignment idle, and a queued delivery started once the reconciler found it idle: about one run a minute whatever the runs took, so a steady stream of events fell hours behind and never caught up. When a poll dispatched the last event of a provider page, the next poll also waited the provider's full poll interval (5 minutes) while more events waited.
- **Now**: a queued delivery starts the moment it is queued and whenever a run ends, up to `ASSIGNMENT_MAX_CONCURRENT_RUNS` (default `10`) runs of one assignment at once, in turns across agents and assignments and within the worker pool (`MAX_CONCURRENT_RUNS`). Polled events always queue: a poll reads up to 5 pages, and a full queue hands the rest back to the poller, which fetches them again later - nothing is dropped. Once an assignment's oldest delivery has waited 5 minutes, fresh deliveries are interleaved with the backlog. After a run meets a provider's HTTP 429 its assignment's cap is halved for a minute (scoped to that assignment).
- **Unchanged**: schedules, manual triggers and follow-ups never start beside an active run (a manual trigger is still refused while one is in flight); a run waiting for human input still holds its assignment's whole queue; pushed events (Slack events, Graph and LinkedIn notifications, provider and agent webhooks) finding every slot taken are skipped unless `INTEGRATION_EVENT_QUEUE_ENABLED=true`; prompts, tools, the activation gate, dry run and loop protection.

An assignment whose runs must not overlap - they rewrite the same shared state, for example - keeps one run at a time with `"max_concurrent_runs": 1` in its `trigger_config` (the assignment API or the agent builder), or serializes only the runs about the same item with `"ordering_key": "<path in the payload>"`. New settings: `ASSIGNMENT_MAX_CONCURRENT_RUNS`, `ASSIGNMENT_DISPATCH_MAX_PENDING_RUNS`, `ASSIGNMENT_QUEUE_BACKLOG_SECONDS`, `ASSIGNMENT_DISPATCH_RATE_LIMIT_BACKOFF_SECONDS` ([Configuration > Webhook delivery](docs/docs/deployment/configuration.md#webhook-delivery)). New `xtmone_assignment_*` queue metrics and a lag alert: `k8s/prometheus-rules/assignment-dispatch-alerts.yaml`.

## Tokens minted on another host tolerate 60 seconds of clock skew

**No action required, no configuration change and no migration.** Every token accepted before is still accepted.

- **Before**: XTM One checked the `exp`, `nbf` and `iat` of a token with no clock tolerance. A cross-platform token from an OpenCTI whose clock ran a second ahead of XTM One's was refused (`XTM JWT rejected: ... The token is not yet valid (nbf)`) and the embedded chat failed with a 401, intermittently. The same held for the A2A Entra and Google federation tokens, and for XTM One's own tokens (session, MFA, MCP OAuth, A2A, presentation export) when one replica or the worker mints a token another verifies.
- **Now**: those checks tolerate 60 seconds of clock difference. A token dated up to 60 seconds in the future, or expired less than 60 seconds ago, is accepted; beyond that it is refused as before. The 15-minute cap on a cross-platform token's lifetime is still read off the token's own claims, so with the leeway on both ends a cross-platform token is accepted for at most 17 minutes of XTM One's clock. The Teams and Google Chat webhooks keep the 5 minutes they already allowed.

Keep the clocks of XTM One and the XTM Suite platforms synchronised (NTP): a clock more than 60 seconds apart is still refused.

## Links to a platform reached on an internal URL are on its public URL

**No action required, no configuration change and no migration.** Where no internal URL is recorded (no `OPENCTI_API_URL` / `OPENAEV_API_URL` / `OPENCRQ_API_URL`, the SaaS shape), nothing changes.

Where XTM One reaches a platform on an internal URL (recorded as described below), that URL stays where XTM One calls the platform, and every link a person is given moves to the platform's registered public URL: the "Open in ..." links of the OpenCTI / OpenAEV / OpenCRQ tools, the platform URL the agents are told to build their links from, the links in their answers (a link on the internal URL, from a conversation older than the upgrade for example, is shown on the public URL, code blocks aside), and `trigger.platform_url` in an assignment prompt. Moving the links of an answer is part of its URL post-processing: with `URL_GUARD_ENABLED=false` an answer is left as written (the tools' results and the listed URL are still on the public URL). Deep links opened from the chat, a channel, a notification or an assignment result no longer point at a host the browser cannot reach. The embedded chat of several instances of one platform (`X-Platform-URL`) also selects the right integration again: either URL names the platform.

## A cross-platform token must carry an expiry and live at most 15 minutes

**Security fix. No action required, no configuration change and no migration.** Every supported OpenCTI, OpenAEV and OpenCRQ version signs its tokens to XTM One with `iat` and `exp`, 10 minutes apart, and keeps working unchanged.

- **Before**: a token signed by a registered platform was checked against its `exp` only when it carried one. A token without `exp` was accepted and never expired, and nothing bounded how far ahead its `exp` could be.
- **Now**: a cross-platform token without `exp` or `iat`, or signed to live longer than 15 minutes (`exp - iat`), is refused with the same 401 as any other invalid token, and XTM One logs `XTM JWT rejected: signature/claim validation failed: ...` with the reason. An expired token is refused as before.

## Cross-platform tokens are verified and addressed by identity on internal URLs

**No action required where every product reaches the others on the URL it registers with.** The keys are fetched and the tokens addressed as before, only compared normalized (below), and the migration `p4880apiurl01` only adds the nullable `platform_registrations.api_url` column.

Where XTM One reaches a platform on an internal URL (`OPENCTI_API_URL`, `OPENAEV_API_URL`, `OPENCRQ_API_URL`, as in the OpenCTI and OpenAEV Docker deployments), the platform's next registration (its heartbeat, a few minutes after the upgrade) records that URL, and from then on:

- XTM One fetches the platform's keys (`/xtm/auth/jwks`) on the internal URL, then on the registered one if the internal URL does not serve them, so the platform's tokens verify inside the container network. Trust is unchanged: only a token signed as the registered URL is accepted.
- The tokens XTM One sends the platform are addressed (`aud`) to its registered URL instead of the internal one: an OpenAEV that checks the audience against its own base URL stops refusing them. A token is addressed that way only when it is sent to the registered URL or the recorded internal URL: an integration `base_url` edited to another host keeps receiving tokens addressed to that host.
- The OpenAEV autonomous run callbacks (status, scope, directives, events) go to the internal URL, addressed to OpenAEV's registered URL.
- Issuers and audiences are compared normalized: a trailing slash, the case of the scheme and host or a default port no longer refuse a token; another host, port, scheme or path still does, and so does a URL carrying credentials or naming port 0.

XTM One publishes its own identity at `GET /xtm/auth/metadata`. A platform that addresses its tokens to the internal URL it reaches XTM One on (`http://xtm-one:4000`) instead of `BASE_URL` keeps being refused until it reads that document; meanwhile set `XTM_AUTH_ACCEPTED_AUDIENCES=http://xtm-one:4000` on XTM One (reported by the Security Audit as `authentication.platform_token_audiences_extra`, logged at startup). Set `BASE_URL` on the worker too. See [Public and internal URLs](docs/docs/deployment/xtm-suite-integration.md#public-and-internal-urls).

## The browser tool checks every redirect against its URL policy

**Security fix. No configuration change and no migration.**

- **Before**: the agent `browser` tool checked the URL an agent asked to navigate to (http or https only, every address the name resolves to globally routable), then let Chromium follow HTTP redirects unchecked. One redirect from a public page reached an address that check refuses - an internal hostname, a private or loopback address, the cloud metadata endpoint - and the page it landed on could be read, captured or scripted by the agent. The redirects of a page's frames, images, scripts and fetch / XHR requests were not checked either.
- **Now**: every redirect hop of every request the tool's browser makes is checked the same way, before it is sent. A hop the check refuses is never requested. A navigation stops there and the tool answers `Navigation blocked by URL policy: redirected to <hop>: <reason>`, with the reason a direct navigate to that hop gets; the page stays on the document it was showing, and a click that led there says the same under its usual result. A refused frame, image or fetch fails to load.
- **Unchanged**: a redirect whose every hop the check allows is followed as before, up to Chromium's own limit of 20 redirects. The first URL of an in-page request keeps the cheap literal-IP filter. The check is the platform's single outbound URL policy, the one `web_fetch` applies to its own redirects, and it has no setting. Each redirect hop costs one DNS resolution, at most four at a time per browser session so a page cannot fill the thread pool the database connections also resolve on; a hop whose address cannot be checked within 10 seconds, waiting included, is refused. The runner's `browser_control`, which drives the browser on a user's own machine, is not affected.

## One agent's unreadable integration bindings no longer stop webhook deliveries for everyone

**No action required and no migration.** A JSON value holding a lone UTF-16 surrogate or `\u0000` escape (a request body's `"\ud800"`, for example) was stored as sent, and every query reading that column as `jsonb` then failed for everyone: one agent's integration bindings holding one made `POST /api/hooks/providers/{provider}` fail for every provider, and the integration-level and HubSpot webhooks for every integration.

- **Such a row is now skipped.** Its agent receives no webhook delivery until its integrations are edited, and the platform says so, at most once every 15 minutes per route and by agent id only: `Provider webhook github: skipped 1 bound agent(s) (<agent id>): their integration bindings hold a \u0000 or lone surrogate escape PostgreSQL cannot read. An edit of the agent's integrations stores them clean.` Every other agent is delivered to exactly as before. The same kind of row no longer fails an entity list either (its group grants read as none), nor variable resolution, the intent catalog, the public catalog's tag filter, the built-in integrations seed, the agent builder's run search or the Security Audit.
- **Such a value is no longer stored**: a lone surrogate written to a JSON column is stored as U+FFFD (a valid pair, such as an emoji, is kept), and one already stored reads as U+FFFD, so an API response carrying it no longer fails.
- **Upgrading across `a2a2intmodel1` or `o1crq2opencrq1`** with such a row no longer fails the upgrade: the migration first stores the row as every JSON write now does (U+FFFD for a lone surrogate) and then applies its own rule to it, like to any other row, printing how many it repaired. A row it cannot read even so gets the restrictive outcome, named by id: `a2a2intmodel1` revokes that agent's live exposure, and `o1crq2opencrq1` stops the upgrade for a deny list (`agents.tool_deny_list`, `platform_settings.disabled_integration_providers`), whose stale entry would stop denying the renamed tool or provider, and leaves any other column under the old name, where it grants less. A number beyond PostgreSQL's numeric range (`1e1000000`, which the platform never writes but a hand-edited row may hold) is decided the same way, by a real cast of that row: readable, it goes through the migration's rule; unreadable, it gets the restrictive outcome.

## Nova `llm:` rules now decide on the full engine

**Behaviour change for installs running the full Nova engine only** (the optional `nova-hunting` package installed, Nova integration connected, rules with an `llm:` section). Installs on the default keyword tier are unaffected.

- **Before**: with nova-hunting 0.3.1 every `llm:` condition failed closed - the matcher passes `temperature=` to the platform's LLM evaluator, which refused it - so a rule resting on its `llm:` tier never matched, although the platform still made the LLM call for it.
- **Now**: the condition is judged at the temperature the rule gives (the pattern's threshold, as Nova intends; `0.6` when the rule sets none) and the rule matches as its author wrote it. On a match the chat check refuses the message for a `block` rule and audits it (`security_alert`, `summary: Nova rule match`) for a `log` rule; the memory-write screen only audits, the memory is stored either way. One LLM call per distinct condition and temperature, as before per condition.
- **Before upgrading**: review the enabled rules with an `llm:` section and their actions (Integrations > Nova > Rules). Rules synced from a repository start as `log` unless an admin raised them; switch a rule to `log` first if you want to watch what it matches before it blocks.
- **Also in this release**: a rule both platform-wide and wired to a run is evaluated once per input instead of twice (its name no longer appears twice in the audit entry's `rules`); the chat check and the memory-write screen wire no rule per run, so their entries are unchanged.

## Verified integration notifications: a GitHub App event reaches an integration recording no installation only if its token sees it

**Nothing changes with Require verified integration notifications off (the default)**: no answer is read, GitHub is asked nothing and every delivery reaches what it reached before. No migration and no new setting.

With the option on (`INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true`), the admin GitHub App's webhook (`/api/hooks/providers/github`) already hands a GitHub App event only to integrations on that app the agent's owner may use and, for an integration recording installations, only for those. An integration recording none - the usual OAuth connection - still received the events of every installation of the app, including organisations its GitHub account cannot see. It now receives one only when its own token sees the event's installation (`GET /user/installations`):

- **A delivery never waits on GitHub**: it reads the answer kept in Redis for the integration's current credentials, and what is missing is asked in the background (paginated, at most 1,000 installations, 4 lookups at a time) for the next delivery.
- **The token sees the installation**: the event is delivered exactly as before. **It does not**: the event is not delivered. That is the leak closed.
- **No answer yet** (the first delivery, one after a token refresh, a reconnect or a configuration edit, after a failed lookup, or while Redis does not answer): the event is delivered as before and the lookup starts. A failed lookup is logged with the integration's id and name and retried a minute later; meanwhile the last answer read for the same credentials, kept 24 hours, decides.
- **A token GitHub lists no installation for** (a `403` that is not a rate limit: a personal access token or an OAuth App token) and **an integration holding no usable token** (none stored, or one that cannot be decrypted) receive no GitHub App event; the second is logged once a day with the integration's id and name. Connect the integration again.
- **Who sees an installation changed**: an `installation`, `installation_repositories`, `installation_target`, `organization`, `membership`, `member`, `team` or `team_add` event marks the installation, and an answer read before the mark no longer decides for it until it is read again, so an organisation that installs the app later is heard at once. A change GitHub does not announce to the app is picked up when the answer is read again, at most 5 minutes and one delivery later.
- **Unchanged**: events naming no installation, integrations recording installations, every other provider and the LinkedIn notification URL.

Before turning the option on, check that every GitHub integration on the admin app is connected. The Security Audit findings `integrations.provider_webhook_unverified` / `integrations.provider_webhook_verified` already report a provider webhook that accepts deliveries without a signature.

## Workset items on a document that cannot be embedded now fail

**Behaviour change for worksets in `auto` processing mode. No migration, nothing to configure.**

The automatic summary and priority pass now only writes to an item whose document is embedded and searchable. Until now it also summarised or promoted an item whose document failed to embed, so the item read `ready` with nothing to search - including short content that yields no usable chunk, which was deliberately accepted before. This reverses that choice:

- **A document that can never be embedded** (an embedding `error` with no retry the failed-document sweeper will run - no usable chunk, no extractable text, the retry budget spent, automatic retries off): its `auto` items end `failed`, with the document's reason and how to finish them by hand (`workset_set_summary` and `workset_set_priority`). Writing both values by hand still promotes them to `ready`.
- **A document that will be retried** (an embedding `error` with a retry the sweeper will run): its items stay `pending` until that retry, and are settled by the pass after it.
- **Unchanged**: items behind an embedded document, and behind a document still `pending` or `processing` (left to its embedding job).

Turning automatic retries off (`EMBEDDING_AUTO_RETRY_ENABLED=false`) or lowering `EMBEDDING_AUTO_RETRY_MAX_ATTEMPTS` under a document's attempts now also fails, on the scheduler's next tick, the items that were waiting for a retry that will no longer run - even while no embedding provider is configured. The same tick also settles items behind a document whose retry schedule the sweeper already cleared once the budget was spent, a bounded page of items per tick, so a large backlog is worked through over successive ticks. Items already `ready` from before the upgrade are left as they are.

## GitHub App installs: rate limited, audited on company-managed rows, recorded by Connect under verified notifications

**No configuration change and no migration.** Follow-ups of the section below; with **Require verified integration notifications** off, a Connect is exactly what it was.

- **An install made from github.com is rate limited**: at most 30 callbacks per client address and 10 per installation every 10 minutes (one install is one callback). Past that, `/api/oauth/callback` answers `429` with `Retry-After`, before GitHub is asked anything.
- **It no longer names an integration in its redirect**: it lands on `/integrations?connected=github`, since the integration it updated may belong to another XTM One user connected with the same GitHub account. An install started from XTM One (Connect) still names its own integration.
- **A company-managed integration with no owner is audited too**: the entry (`system:github_app_install`) is anchored on whoever connected its token, else the oldest admin, as for the platform's other actions on company-level configuration. A platform with no admin at all logs a warning instead.
- **A token refreshed while GitHub is asked no longer loses the installation**: the new token is asked once more and the installation is recorded if it is still the installer's account on the same app. Every integration left alone is logged with the reason.
- **A failed install is shown**: the Integrations page says why (`github_token_exchange_failed`, `github_no_token`, `no_github_credentials`), and an integration's page says when its OAuth authorization failed.
- **With Require verified integration notifications on, Connect records the installations its new token sees** on a GitHub integration that already records installations (it keeps the ones it records). An integration connected with a bot account thus receives an installation someone else made once it is connected again. An integration recording no installation is left as is.

## Runner enrollment checks its transport before its body

**No action required, default unchanged.** With **Require HTTPS for runner enrollment and MCP tokens** off (the default), `POST /runners/enroll` answers exactly as before. With it on, the transport is now checked before the request body is read, as the documentation already said: an enrollment over a transport the platform cannot confirm encrypted answers `403 {"detail": "https required"}` for a malformed JSON body too, where it answered FastAPI's `422` first. Every other answer is unchanged, and nothing was ever issued on either answer.

## MCP client registration joins the opt-in HTTPS switch

**No action required, default unchanged.** With **Require HTTPS for runner enrollment and MCP tokens** off (the default, `CREDENTIAL_TRANSPORT_STRICT=false`), `POST /mcp/oauth/register` answers exactly as before, status, headers and body. Like runner enrollment and the MCP token endpoint, a registration whose transport the platform cannot confirm encrypted is now also logged once per process (`/mcp/oauth/register served over unconfirmed transport ...`), so the check below still names every request the option would refuse.

- **With the option on**, a registration over such a transport is refused before its body is read and before anything is issued, in RFC 7591's error shape: `400 {"error": "invalid_client_metadata", "error_description": "https required"}`. A confidential client receives its `client_secret` in the registration answer, which is why it belongs with the token endpoint: an MCP client refused here would have been refused at the token endpoint anyway, since both are advertised on the same base URL.
- **Before turning it on**: unchanged - check that no `served over unconfirmed transport` warning is left in the log.
- **Security audit**: the ids `network.credential_transport_strict`, `network.credential_transport_strict_off` and `network.credential_transport_strict_on` are unchanged; their texts now name the registration route too.

## A GitHub App installed from GitHub's own page no longer connects an integration

**No configuration change and no migration.** When a GitHub App is installed from github.com (with "Request user authorization (OAuth) during installation"), GitHub sends the browser back to XTM One without saying which XTM One user or integration the install is for. That callback used to store the installer's GitHub token on the first GitHub integration it found - a waiting one, else a company-managed one, else the most recent one, whoever owned it. It now stores no token anywhere:

- The installation is added only to the connected integrations on the same GitHub App whose own token is the installer's GitHub account and that already record installations. Each such change is in the audit log, under `system:github_app_install`.
- A waiting (`configured`), `error` or `reauthorize` GitHub integration is connected with its own Connect button, as the GitHub setup guide says; after such an install the Integrations page tells the installer so.
- An install started from XTM One (Connect) and installation updates are unchanged.

## Opt-in: runner enrollment and MCP tokens over confirmed HTTPS only

**No action required and no behaviour change.** `POST /runners/enroll` and `POST /mcp/oauth/token` answer exactly as before: a request whose transport the platform cannot confirm encrypted is served and logged once per route (`... served over unconfirmed transport ...`, with the peer and the forwarding headers it saw).

- **Opt-in: refuse it.** **Require HTTPS for runner enrollment and MCP tokens** in Settings > Security > Security options, or `CREDENTIAL_TRANSPORT_STRICT=true` (the variable, when set, wins and locks the field), makes both refuse such a request the way the A2A surface does, before reading it or issuing anything: `/runners/enroll` answers `403 {"detail": "https required"}` (XTM One Desktop and the headless runner then say the server could not confirm HTTPS and name the settings to check) and `/mcp/oauth/token` answers `400 {"error": "invalid_request", "error_description": "https required"}` (RFC 6749 section 5.2). Real loopback stays exempt under `DEBUG` or `A2A_ALLOW_INSECURE_LOOPBACK`, as on the A2A surface.
- **Before turning it on**, behind a TLS-terminating proxy, name the proxy in `FORWARDED_ALLOW_IPS` (see *Set `FORWARDED_ALLOW_IPS` behind a TLS-terminating proxy* below) and check that the `served over unconfirmed transport` warning no longer appears: every request it would name is one the option refuses.
- **Security audit**: the new `network.credential_transport_strict` control is off by default, reported by the info finding `network.credential_transport_strict_off`, which recommends the option; with it on, the pass `network.credential_transport_strict_on`. The overall score and the administrators' notifications are unchanged (they follow warning and critical findings).

## The trace storage and payload switches now apply

**Applies to every deployment where Settings > Observability has `Enable local
trace storage` or `Store full payloads` off. No migration, nothing to
configure; with both on (the default), traces are recorded exactly as before.**

Both switches were saved and shown, and read by nothing: every LLM trace was
stored with its prompts, answers, tool arguments and tool results whatever they
said. They now apply to the native trace store (PostgreSQL, and ClickHouse when
it is the store), within a few seconds on every server and worker:

- **`Store full payloads` off**: new traces keep their timings, token counts,
  cost, model, trace and tool names, status, failure cause and metadata, and no
  longer the prompts, answers, tool arguments or tool results; an error message
  keeps its first 200 characters. The `Traces` page shows those traces with
  empty input and output panes; analytics and cost reports are unchanged.
- **`Enable local trace storage` off**: no new trace, span or user feedback
  score is written; the `Traces` page and the trace-based analytics stop
  growing. An administrator's own score on a trace already stored
  (`POST /api/v1/traces/{id}/scores`) is still recorded.
- **Traces already stored** keep what they hold until the retention period
  deletes them; delete individual traces to remove them sooner.
- **The Langfuse export is not governed by the switches**: while it is on, it
  still receives each call's payloads. Turn it off as well to keep payloads
  inside the platform.
- **During the rolling update**, a replica still on the previous release keeps
  storing what it did until it is replaced; a trace it spilled to the Redis
  overflow is written under the switches by an upgraded replica.
- **Security audit**: the switches are the `llm.trace_payloads` control,
  enforced while either is off.

## Administrators are told of new Security Audit findings, dormant accounts' automations included

**No action required and no configuration change. One migration runs**: it adds one partial index to `audit_logs`, `ix_audit_logs_sign_ins`, holding the completed sign-ins only. It is built `CONCURRENTLY` at startup, so audited writes keep going while it builds (at 2.5 million audit rows: 17 MB, half a second), and bounded like every concurrent build, so startup never waits on it indefinitely. A build interrupted by a restart is rebuilt at the next start; there is nothing to drop by hand.

- **Once an hour the platform runs the Security Audit and tells every active administrator, in the bell, of the critical and warning findings not reported yet**: one notification per run, listing their titles. The first run after the upgrade therefore sends one notification with the findings the audit reports today; afterwards only new findings, escalations from warning to critical, and findings back after a day without them.
- **A new warning, `authentication.dormant_account_automations`, lists the active accounts nobody signed in to for 30 days that own a running automation**: an enabled assignment on an enabled agent, or an active flow, not company-managed. Deactivating such an account in Admin > Users pauses them; reactivating it resumes them unchanged.
- **A declined XTM One Desktop sign-in request ("Not my machine") is audited as `login_declined`**, no longer as `login`, which every reader of the audit log takes for a sign-in. Rows already recorded are unchanged.

## A busy assignment's integration events are skipped again

**Applies to every deployment that ran a build that queued such events. Nothing to
configure.**

An integration event (a polled message, a Slack event, a Graph or
LinkedIn notification, a provider or agent webhook) reaching a busy assignment
was queued as a `queued` run instead of being skipped. On a high-volume trigger
the queue grew faster than it drained, and the drain could stop reaching an
assignment at all once 20 others were held by a long or paused run. Such an
event is skipped again by default; `INTEGRATION_EVENT_QUEUE_ENABLED=true` keeps
queueing it. The drain now only counts the assignments it can promote.

The runs already `Queued` are promoted one at a time, oldest first, once their
assignment is idle. To drop a stale backlog instead, an administrator deletes
every queued run of an agent with `POST /api/v1/assignments/runs/bulk-delete`
and `{"all_matching_filters": true, "status": "queued", "agent_id": "<agent id>", "scope": "all"}`,
or selects the runs marked `Queued` in Operations > Executions filtered on the
agent. That list has no `Queued` filter, so never use its "select all matching"
for this: it would delete the agent's whole run history.

## Client addresses are validated, with stricter keys available by opt-in

**No action required and no configuration change.** Every reader of the client address (per-IP rate limiting, the audit trail, the webhook diagnostics, the access log) now goes through one check, and every address a proxy writes is read exactly as before. Which proxy may supply the client address and the scheme does not change, for any combination of `TRUST_PROXY_HEADERS` and `FORWARDED_ALLOW_IPS`.

- **A value that is not an IP address is ignored.** Only a client writes one. Under `TRUST_PROXY_HEADERS`, a leftmost `X-Forwarded-For` value that is not an IP address in any form falls back to the peer. A peer that is not an IP address (uvicorn copies it from `X-Forwarded-For` unchecked, which any caller can arrange under `FORWARDED_ALLOW_IPS=*`) is recorded as no address: `NULL` in the audit trail, `client_ip: null` in the access log, and one shared `unknown` rate-limit bucket that no request carrying an address joins. The platform logs a warning the first time it ignores one. That text used to be keyed on and recorded verbatim, and past 45 characters it failed the audited action with a 500. An address in any form a proxy writes - `IP:port`, `[IPv6]:port`, a zone index, uppercase IPv6, several hops, extra spaces - and the `unknown` a proxy writes for a client it hides keep their rate-limit key and audit value; only an address longer than the audit trail's 45 characters, which failed the audited action the same way, is now stored in its canonical form.
- **Opt-in: canonical client addresses.** **Canonical client addresses** in Settings > Security > Security options, or `CLIENT_IP_STRICT=true` (the variable, when set, wins and locks the field), keys, logs and stores every client address in canonical form: the port, brackets and IPv6 zone index dropped, an IPv4-mapped address read as IPv4, IPv6 lowercase and compressed. One client is then one rate-limit key whatever the proxy wrote: behind a proxy that writes `IP:port` into `X-Forwarded-For` under `TRUST_PROXY_HEADERS` (Azure Application Gateway does), every connection is otherwise its own bucket. A client whose address changes spelling has its current window restart once when the setting is switched on.
- **Opt-in: rate-limit native IPv6 clients per network.** **IPv6 rate-limit prefix length** set to `64` in Settings > Security > Security options, or `RATE_LIMIT_IPV6_PREFIX=64` (the variable, when set, wins and locks the field), keys a native IPv6 client on its /64 (`2001:db8:1:2::/64`) instead of its address, on every per-IP limit: login, forgot and reset password, invitation registration, MFA verification, A2A pre-authentication, MCP OAuth client registration, authorization and token, the public chat widget, and Content Security Policy violation reports. A host or a site is handed a whole /64, so a per-address key lets one client draw a fresh bucket for every request from an address it owns. With the setting, every address of one /64 shares one budget - every device of a corporate IPv6 LAN included - so enable it where IPv6 clients do not share a /64 at scale. The default, `128`, keeps one bucket per address; the platform says which key it uses in one startup log line, and the security audit reports an info finding recommending `64` while the value is above `64`. IPv4 keys and the address recorded by the audit trail, the access log and the webhook diagnostics are the same under every value.

Existing audit rows are untouched; there is no migration.

## `FORWARDED_ALLOW_IPS` behind a reverse proxy

**No behaviour change and no action required.** A deployment with `TRUST_PROXY_HEADERS=true` keeps reading the client address and the scheme from the same headers and peers as before; the address checks of the section above apply to it as to every deployment. The flag is also the **Trust proxy headers** field of Settings > Security > Security options; the variable, when set, wins and locks it. While the flag is on, the platform now logs one warning at startup, or when the field is switched on, and the security audit reports "Forwarded headers are trusted from any peer" (`GET /api/v1/security-audit`) with the recommended setting.

Why: the flag reads `X-Forwarded-For` and `X-Forwarded-Proto` from any peer, takes the client from the leftmost `X-Forwarded-For` value - which the client itself writes behind a proxy that appends to the header (nginx's `$proxy_add_x_forwarded_for`, Traefik or ingress-nginx trusting an upstream header) - and wins over `FORWARDED_ALLOW_IPS` when both are set. It suits only a proxy that overwrites the header and is the only thing that reaches the platform. `FORWARDED_ALLOW_IPS` has uvicorn honour the headers only from the proxies it names and read the chain from its end, which closes that gap.

To switch to `FORWARDED_ALLOW_IPS` (recommended, whenever convenient), one step at a time. The flag keeps applying until step 5, so the trust list is checked before anything relies on it:

1. Read the proxy's address in the access log: the `client_ip` field of any request is the peer the platform sees.
2. Keep the proxy sending both headers, with a single `X-Forwarded-Proto` value:
    - **nginx**: `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` and `proxy_set_header X-Forwarded-Proto $scheme;`. With the Compose stack the peer is the Docker bridge gateway (for example `172.18.0.1`) or, for nginx in the same Compose network, its container address or subnet.
    - **Traefik**: nothing to add, it sends both. Name Traefik's address or its Docker network's subnet; a load balancer in front of Traefik belongs in the entry point's `forwardedHeaders.trustedIPs`, not in the platform.
    - **Kubernetes ingress**: the peer is the ingress controller pod, so name the cluster's pod CIDR (for example `10.42.0.0/16` on k3s), or the node addresses for a controller on the host network, in the platform container's environment. With the Helm charts, that is the platform's `env` map (example in the next section).
3. Set `FORWARDED_ALLOW_IPS=<your proxy addresses>` and restart: an IP, a CIDR or a comma-separated list, listing every proxy hop between the user and the platform. Hostnames are not resolved and a CIDR needs its host bits at zero; an entry that is neither an IP address nor a network trusts nobody, and the platform names it in a startup warning. Not `*`, which trusts every peer and reads the caller-controlled start of the chain again.
4. Check, with the flag still on: browse to the platform and read your request's access-log line. `client_ip` is what the platform uses once the flag is off and must now show your own address, not the proxy's (`forwarded_for` is what the flag reads today). If it still shows the proxy, the list does not name it: correct it and restart before going on.
5. Turn the flag off where it is set: remove `TRUST_PROXY_HEADERS` (or set it to `false`) and restart, or, when the variable is not set, turn off **Trust proxy headers** in Settings > Security > Security options.
6. Check: no warning about the flag at startup and no "Forwarded headers are trusted from any peer" warning in the security audit; `forwarded_for`, written only under the flag, disappears from the access log.

Full examples: [Installation > Client address and protocol behind the proxy](docs/docs/deployment/installation.md#client-address-and-protocol-behind-the-proxy).

## Triggers can match exactly the event they select

**Applies to every deployment with integration-event or webhook assignments.
No migration, nothing to configure: every existing trigger keeps running on
exactly the events it runs on today.**

An integration-event trigger (and the `events` list of a webhook trigger)
matched the delivery's event key by prefix, so a selected event also ran on
every event whose name starts with it: `pull_request` also ran on
`pull_request_review`, `pull_request_review_comment` and
`pull_request_review_thread`, `repository` on `repository_dispatch`, and a
GitHub trigger on its dedicated URL on every delivery, `ping` included.
**Exact matching** runs a trigger on the event it selects and that event's
actions only (`pull_request`, `pull_request.opened`, ...).

- **Which triggers match exactly**: a trigger created on this version (the
  trigger editor, the REST API, the agent builder, an agent template), and a
  trigger whose events are chosen again - another event, provider, `events`
  list, `path` or trigger type.
- **Which keep the prefix match**: every trigger saved before this version,
  through every other edit (name, prompt, delay, filter, integration, enabled,
  schedule, a bulk update, an agent-builder `update_assignment` that leaves the
  events alone) and through every copy (agent duplicate, import, catalog
  clone), which keeps its source's mode.
- **How to see it**: the assignment's `Trigger` card shows `Exact event
  matching` and what it runs on, or `Legacy event matching` and the events it
  also runs on by prefix; the trigger editor says before you save when a
  change switches it. The assignment API returns `event_matching` (`exact` /
  `legacy`), the first delivery a legacy trigger runs on only by prefix is
  logged (`legacy event matching ran it on ...`, once per assignment and
  process), and the security audit reports `assignments.legacy_event_matching`
  (info) or, once every event trigger matches exactly,
  `assignments.event_matching_exact` (pass), under the new
  `assignments.exact_event_matching` control.
- **How to switch one trigger**: `Use exact matching` on its `Trigger` card,
  or choose its events again in the trigger editor.
- **How to switch every trigger**: Settings > Security > Security options >
  `Exact event matching for every trigger`, or
  `WEBHOOK_EXACT_EVENT_MATCHING=true` (the variable, when set, wins and locks
  the field). It only narrows what a trigger runs on.

Integration-event triggers the agent builder created before this version
stored their event under `event_type`, a key the platform does not read:
such a trigger runs on every event its provider delivers by webhook, and a
polled one (a new email, a new message) never runs. They keep behaving that way
- the global switch included - until `Use exact matching` is chosen on the
trigger's own card: it then runs only on the event it names, and a polled one
starts with its next new event instead of the provider's latest page. The
`Trigger` card and the security audit (`assignments.legacy_event_type_key`)
point them out. The agent builder now stores `event`.

What changes without any action is limited to values that could not work: a
trigger config that is not an object, a non-empty `event` that is not a string
(a list, a number) and an `events` allow-list of numbers raised an error on the
delivery, failing it for every other assignment of the fan-out; such a trigger
now matches no delivery and is logged, and the others run.

## Teams private-channel notifications check their subscription

**No action required, nothing to configure.** No migration, no new setting, no Microsoft Graph permission, consent, app-registration setting, URL, bot reinstall or manual re-subscription. Every Microsoft Graph notification of a private or shared Teams channel that an existing install acts on today is still acted on, and every subscription it holds is kept and renewed.

**What an existing install sees after upgrading, with nothing changed:**

- **Subscriptions created before 2026-07-02** (their clientState is `copilot_` followed by the team id): kept and renewed as before, their notifications acted on as before.
- **Subscriptions created from 2026-07-02** (a random clientState stored in plaintext): the maintenance pass re-stores the value encrypted, in place, with no Graph call.
- **New subscriptions**: a random clientState stored encrypted, and a lifecycle notification URL - the same public endpoint, `<BASE_URL>/api/channels/teams/graph-notifications`, with `?team=&channel=` - which Microsoft requires for a Teams subscription longer than an hour. A `reauthorizationRequired` lifecycle notification renews the subscription, a `subscriptionRemoved` one creates a new one.
- **Notifications that do not prove the subscription recorded for their channel but that earlier releases acted on** (the derivable value for a channel with no record, an unreadable one or one from before 2026-07-02; the channel's clientState under another subscription id or tenant; after a `SECRET_KEY` change, a notification naming the channel's recorded subscription): acted on as before, with a warning in the log (at most once every 15 minutes per case, never the payload) and a Security Audit finding.
- **Messages no subscription notified** (a subscription Microsoft removed, one recreated because its record was lost, a notification refused on a monitored channel whose record is missing or unreadable): read back from Microsoft Graph once the channel has a subscription again, up to four hours back, and answered once. A message Graph notifies twice is answered once.
- **The maintenance pass** runs on the first scheduler tick after a start, then hourly (it used to wait for the first hour).
- **A malformed notification body** is acknowledged (it was a `500`, which only makes Graph retry).
- **Rolling deploy and rollback**: an older replica reads an encrypted record as one without a clientState and refuses the notifications of the subscriptions this release created or re-encrypted. Those notifications are thread replies, which no earlier release answers (a reply's resource was read as its root message), so nothing an older replica would answer is lost; the thread-reply handling must ship with or after this change.

**What the new warnings and Security Audit findings mean** (Settings > Security; the switch they relate to is listed there as the `integrations.notifications_require_verification` control, off by default - the switch of the next entry, which covers these notifications too):

| Finding | Severity | Meaning | What to do |
|---|---|---|---|
| `channels.teams_graph_notifications_unverified` | warning | A Teams bot is configured and `/api/channels/teams/graph-notifications` still accepts notifications that do not prove their subscription, as earlier releases did. It counts the monitored channels, those whose subscription uses the derivable clientState, those whose record cannot be read or decrypted and those with no record, and the notifications accepted that way in the last 7 days. | Turn verification on when you are ready (below). |
| `channels.teams_graph_notifications_verified` | pass | Verification in force. | - |

**To turn verification on:** turn on **Require verified integration notifications** in Settings > Security > Security options, or set `INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true` (the variable wins and locks the field). It is one switch for every notification the platform cannot verify: it also refuses the unverified Slack, Microsoft Graph and LinkedIn notifications of integrations (next entry). Every Teams channel notification that does not prove its subscription is then refused, and the next maintenance pass replaces the subscriptions whose clientState is derivable or cannot be decrypted. Microsoft allows one subscription per channel, so each is deleted before its replacement is created: what it was still delivering is accepted for four hours (Graph's retry horizon) and what was posted in between is read back from Graph. If Graph refuses to create a replacement, that channel's follow-ups stop until a later pass creates it, and the log says `Graph subscription creation failed`. Turning the option off restores the default within seconds; a subscription already replaced stays replaced.

**If a notification is refused**, see [Microsoft Teams > Graph notifications for private channels](docs/docs/administration/channels/microsoft-teams.md#graph-notifications-for-private-channels).

## Slack, Microsoft Graph and LinkedIn notifications check their sender

**No action required, nothing to configure.** No migration, no new required setting, nothing to change in the Slack app, the Entra app registration or the LinkedIn app, no new URL. Every delivery an existing install receives today keeps arriving: a sender is checked only where the platform holds what proves it (a Slack Signing Secret you choose to store, a Graph subscription recorded with a `clientState`, the LinkedIn client secret it already used), and anything it cannot check is accepted as before and reported.

**What an existing install sees after upgrading, with nothing changed:**

- **Slack events** (`/api/v1/integrations/notifications/slack`): no Slack Signing Secret could be stored before this release, so every delivery is accepted and dispatched as before, and the URL verification handshake is answered. A warning in the log (at most once every 15 minutes) and a Security Audit finding say that events are accepted without a verified signature.
- **Microsoft Graph change notifications** (`/api/v1/integrations/notifications/microsoft`): a subscription record holding only the assignment id - the only shape this endpoint read; no platform code writes one - keeps working, with a warning in the log.
- **LinkedIn notifications**: a configured LinkedIn client secret still refuses a missing or wrong `X-LI-Signature` (`401`), as before; with none configured a notification is accepted as before, with a warning and a Security Audit finding. A client secret that cannot be decrypted now answers `503` instead of `500`.
- **The admin OAuth app's webhook** (`/api/hooks/providers/<provider>`): a verified delivery reaches every assignment it reached before. The ones the strict rule would leave out are logged, and the Security Audit says so.
- **On every install:**
    - The real-time Slack path applies the trigger's channel selection, as the poller always did (it read only the legacy `filter.channels` key before).
    - A Slack message runs an assignment once, whether it arrives in real time, through the poller or as a Slack retry.
    - A message or email an assignment's own run posted (Slack, Teams channel, Outlook send or draft) no longer triggers that same assignment again; every other assignment still runs on it.
    - An integration event that reaches a busy assignment is skipped, as before. With `INTEGRATION_EVENT_QUEUE_ENABLED=true` it is queued instead: a polled event meeting a full queue is fetched again on a later poll, a pushed event is queued past the cap with a warning, and the queued events run one at a time - off by default, because a high-volume trigger's backlog grew faster than it drained. The per-assignment webhook (`POST /api/hooks/assignments/{id}`) keeps its `ASSIGNMENT_QUEUE_*` behaviour.
    - A notification body over 4 MiB is answered `413`, a malformed one `400` (it was a `500`).

**What the new warnings and Security Audit findings mean** (Settings > Security; the switch they relate to is listed there as the `integrations.notifications_require_verification` control, off by default):

| Finding | Severity | Meaning | What to do |
|---|---|---|---|
| `integrations.slack_events_unverified`, `integrations.slack_integrations_unverified` | warning | Slack events are accepted without a verified signature for integrations whose app Signing Secret is not stored. | Store the app's Signing Secret (below). |
| `integrations.linkedin_notifications_unverified` | warning | LinkedIn assignments exist and no LinkedIn client secret is configured, so notifications are accepted unverified. | Configure the LinkedIn app in Settings > OAuth defaults. |
| `integrations.admin_app_scope_not_applied` | info | Admin-app deliveries still reach every assignment they reached before. | Nothing, unless you want the strict rule. |
| `integrations.slack_signing_secret_unusable` | warning | A stored Signing Secret cannot be decrypted or is not text, so it verifies nothing. | Re-enter it. |
| `integrations.slack_events_verified`, `integrations.notifications_verified` | pass | Verification in force. | - |
| `integrations.slack_integrations_polled`, `integrations.linkedin_notifications_dropped` | info | Only with the option on: what it costs (integrations polled instead of real time, LinkedIn assignments that cannot trigger). | Store the Signing Secret, configure the LinkedIn app. |

**To turn verification on:**

1. **Per Slack app**, store its Signing Secret (Slack app > `Basic Information`): in the integration's `Signing secret` field, or in the Slack OAuth default's for integrations created on the administrator's app. From then on, a delivery that secret does not prove never reaches an assignment connected only to that app's integrations, and a verified event reaches only the integrations of the workspace it was sent for (and, outside public channels, of its author or of the user Slack authorized it for); other users still get those messages from the poller within 5 minutes. The first verified delivery asks Slack once which workspace each integration belongs to (`auth.test` with the integration's own token, cached per token).
2. **To refuse everything that cannot be verified**, turn on **Require verified integration notifications** in Settings > Security > Security options, or set `INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true` (the variable wins and locks the field). It refuses unverified Slack events (`401`; those integrations are then polled), Graph records without a `clientState` and LinkedIn notifications when no client secret is configured, and limits the admin app's webhook and the LinkedIn notifications to the admin app's integrations the agent owner may use. Read the warnings above first: they are what the option would refuse.

**If a delivery is refused**, see [Troubleshooting > Real-time Slack, Microsoft Graph or LinkedIn events do not arrive](docs/docs/deployment/troubleshooting.md#real-time-slack-microsoft-graph-or-linkedin-events-do-not-arrive). Turning the option off restores the previous behaviour within seconds.

## The admin OAuth app's webhook says which deliveries it cannot verify

**No action required, nothing to configure, no change in behaviour.** Every delivery `/api/hooks/providers/<provider>` accepted before is still accepted, answered the same way and handed to the same assignments, whether **Require verified integration notifications** is on or off. No migration, no new setting.

That URL verifies a delivery with the webhook secret of the provider's OAuth default (for LinkedIn, its client secret). For a provider with no OAuth default, or with one that holds no webhook secret, a delivery is accepted without a check, as before signatures were checked: anyone who can reach the URL can send an event that triggers the assignments it reaches. This is now reported: a warning in the log once such a delivery triggers assignments (at most once every 15 minutes per provider), and a Security Audit finding naming each provider, why its deliveries cannot be verified and how many active assignments they can trigger.

| Finding | Severity | Meaning | What to do |
|---|---|---|---|
| `integrations.provider_webhook_unverified` | warning | Active assignments can be triggered through `/api/hooks/providers/<provider>` by a delivery the platform cannot verify. | GitHub or Slack: set the app's webhook secret in Settings > Integrations > OAuth Defaults. A provider with no OAuth default: turn on **Require verified integration notifications** (Settings > Security), under which it reaches nothing. A provider whose OAuth default cannot hold a webhook secret: nothing sends its events to this URL, so block it at your reverse proxy. |
| `integrations.provider_webhook_verified` | pass | Every provider whose deliveries can trigger an assignment verifies them. | - |

## The allowed email domains now restrict SSO account creation

**Applies to every deployment that sets Allowed email domains (Settings >
Security > Authentication) and signs people in through OIDC or SAML with
On-the-fly user creation on. No migration, nothing to configure.**

The allowed email domains restricted local registration only. An OIDC or SAML
sign-in created an account for any email address the identity provider
asserted, whatever its domain: with a multi-tenant provider (Google, a
Microsoft Entra app that accepts any tenant), anyone that provider could sign
in.

SSO account creation now applies the list, OIDC and SAML alike, as the setting
always promised: an identity that matches no account and whose email domain is
not in the list is refused before any account is created, and returns to the
login page with `?sso_error=not_provisioned`, as an identity that is not
provisioned does. The domain is compared exactly and case-insensitively, as
registration compares it.

- **What you may notice**: a person outside the listed domains who used to get
  an account at their first SSO sign-in is now refused, and the server log says
  why (`its domain is not one of the allowed email domains`). Create their
  account first (Users > New user, or an invitation), or add their domain to
  the list.
- **Existing accounts are unaffected**: an account that exists keeps signing in
  whatever its domain, and an SSO identity is still linked by email to an
  existing account.
- **Automatically admin users is unchanged**: it still promotes everyone who
  signs in through that method, at every sign-in, existing accounts included,
  and the settings page now says so. The allowed email domains narrow who gets
  an account, not who is promoted.
- **Security audit**: `authentication.sso_account_creation` is enforced once
  the allowed email domains are set, and the
  `authentication.sso_open_account_creation` warning is raised only while SSO
  creates accounts with no domain list.

## A `computer_use` call now waits as long as the machine may take to report

**Applies to every deployment that does not set
`RUNNER_RESULT_WAIT_TIMEOUT_SECONDS`. No migration, nothing to do.**

The `computer_use` tools waited a fixed 180 s for a command's result. A
machine may run a command for its whole execution limit
(`RUNNER_COMMAND_DEFAULT_TIMEOUT_SECONDS` + 30 s, 150 s by default, the wait
for its turn on the machine included) and then retry the result upload for
50 s while the platform restarts, so a valid result could land at about 200 s:
it was recorded in the machine's command history, and the agent was told there
was no result.

`RUNNER_RESULT_WAIT_TIMEOUT_SECONDS` is now unset by default, which means the
wait covers that whole window, counted from the command's delivery to the
machine, plus 5 s: 205 s with the default limit, and it follows
`RUNNER_COMMAND_DEFAULT_TIMEOUT_SECONDS` if you change it. It still ends 5 s
before the command's lease runs out, so a command the wait gives up on is
never sent to the machine again.

- **What you may notice**: an agent waits up to 25 s longer on a command that
  never reports, and a raised `RUNNER_COMMAND_DEFAULT_TIMEOUT_SECONDS` now
  lengthens the wait with it instead of being cut at 180 s.
- **The agent run's limit for one tool call still applies** (300 s at most,
  less in a long assignment run or near a run's deadline). When it ends the
  wait first, the tool stops 5 s before it and tells the agent the command may
  still finish, with its output then in the machine's command history, instead
  of the agent loop reporting a bare tool timeout.
- **A value you set keeps its meaning**: a fixed wait of that many seconds from
  dispatch, as before. Set `RUNNER_RESULT_WAIT_TIMEOUT_SECONDS=180` to keep the
  previous behaviour.

## Quota rules for values saved from now on

**Nothing to do. No configuration change and no migration: every quota value
an existing deployment has stored keeps exactly the effect it has today.**

The user, group and platform quota editors follow one set of rules for every
value saved from this release on:

- `0` is a hard stop, including under an XTM license quota or the included
  allowance of a connected Enterprise Edition platform.
- An empty user or group field inherits: the user falls back to their groups,
  then to the per-user default.
- Unlimited is an explicit switch in the user and group editors, stored as
  `{"unlimited": true, "default": null, ...}`, and an Unlimited group outranks
  every group limit.

Every save stamps each quota type it writes with `"rules_version": 2`. A stored
value without it was saved before this release and is read with the rules it
was saved under, decided at read time by every reader (the quota check, the
usage displays, the admin monitoring view and the agent failure pool): a `0`
under the license pool still gives way to the pool, a group without a limit
still yields to a sibling group with a number, and a user or group quota type
holding no limit still counts as Unlimited at its tier.

Such a value only changes when an administrator saves it. Where saving it would
change what it does, the quota screens (Settings > Quotas, and the Quotas tab
of a user or a group) say so on that value, in every language, and the
platform logs one warning per process for each of those older rules still in
use.

API notes:

- Request bodies keep their meaning. On `PUT /api/v1/users/{id}/quotas` and
  `PUT /api/v1/groups/{id}`, a quota type holding no limit (such as
  `{"default": null}`) is still Unlimited; the editors inherit by leaving the
  type out, and a client may send `"unlimited": false` for the same. On
  `PUT /api/v1/groups/{id}`, `"quota_config": null` or an omitted field still
  leaves the override as it is, and `{}` still clears it.
- `GET /api/v1/groups/{id}` returns the group's `quota_config` (the group quota
  tab used to open empty).
- `GET /api/v1/platform/config` reports a license period without a cap as
  `null` in `agentic_quotas` (and `xtm_license.agentic_quotas`) instead of `0`;
  `0` only means no allowance (Community Edition). A license period issued with
  a quota of `0`, or without a quota claim, still has no cap: the certificate
  is read as before.
- A `platform_limit` of `0` is stored and reported as `null`, which is what it
  always meant.

## Word documents are now read with their tables, headers and footers

**Applies to every deployment at rollout. No configuration change is
required.** The `.docx` text extraction used to read only the body's
top-level paragraphs: every table cell and every page header / footer was
silently dropped, in chat attachments, `read_file_content` and knowledge-base
ingestion alike. Tables now arrive as `| a | b |` rows in reading order,
headers and footers once each.

**Each synced `.docx` that contains a table, header or footer is re-embedded
once.** Knowledge-base sync detects changes by hashing the extracted text, so
on the first sync after the upgrade those documents read as changed and go
through the embedding queue again - an embedding-provider cost proportional
to their size, spread over the normal sync schedule. Documents with plain
paragraphs only are unchanged and are not re-embedded.

**Documents uploaded by hand keep the text extracted at upload time**
(re-embedding rebuilds the index from that stored text). Upload them again to
make their tables searchable.

## Set `FORWARDED_ALLOW_IPS` behind a TLS-terminating proxy

**Action required on any deployment that terminates TLS at a proxy.** Without
it the platform sees every request as plain HTTP from the proxy's address.

Set it to the proxy's address, a CIDR, or a comma-separated list:

```
FORWARDED_ALLOW_IPS=10.42.0.0/16
```

Neither Helm chart sets it yet (nor `TRUST_PROXY_HEADERS`), so a chart install behind an ingress sees every request as plain HTTP from the ingress controller pod, as in earlier releases, until the variable is set through the platform's `env` map, to the cluster's pod CIDR:

```yaml
xtmOne:            # helm-xtm-one; the key is xtmone in helm-xtm-suite
  platform:
    env:
      FORWARDED_ALLOW_IPS: "10.42.0.0/16"
```

**Per-IP rate limiting buckets every caller under the proxy's address** until
it is set, so the limits on `/auth/login` and `/auth/forgot-password` are
shared platform-wide.

**The A2A surface refuses a request it cannot confirm encrypted**
(`403 {"detail": "https required"}`) - the agent card, partner discovery, the
JSON-RPC task surface and delegation callbacks - as release 1.260917.0 did.
The difference is that a proxied deployment can now make it work: set
this variable and the A2A endpoints answer over HTTPS behind the proxy.

- **Who is affected**: a deployment that terminates TLS at a proxy it does not
  trust - neither this variable nor `TRUST_PROXY_HEADERS` set, which is the
  Helm charts' default and a Compose stack's behind an external proxy - and a
  trusted proxy that sends no `X-Forwarded-Proto`. Such a deployment answered
  403 in 1.260917.0 too; an unreleased build in between served it with a
  warning, so one upgraded from that build sees its A2A partners refused again.
- **What you see**: partners get the terse 403 (delegation callbacks answered
  `{"error": "https required"}` in 1.260917.0); the platform logs each refused
  route once per process with the peer and the forwarding headers it saw, and
  the security audit (`Settings` > `Security`) reports "No reverse proxy is
  trusted to confirm HTTPS" for an `https` `BASE_URL`. `refused over unconfirmed transport
  (scheme=http, peer=10.42.3.7, x_forwarded_proto=https,
  x_forwarded_for=present)` is a proxy the platform does not trust, at
  `10.42.3.7`. `x_forwarded_proto=none` with `x_forwarded_for=present` is a
  proxy that sends no `X-Forwarded-Proto`: it may already be trusted, and
  `peer` is then the client it named - never an address to trust.
- **The fix**: set this variable to the proxy's address as the platform
  container sees it (the `peer` of an `x_forwarded_proto=https` line), and
  have the proxy send `X-Forwarded-Proto`. On Kubernetes,
  name the ingress controller's pod network in the platform container's env -
  `xtmOne.platform.env` in `helm-xtm-one`, `xtmone.platform.env` in
  `helm-xtm-suite` - until the charts gain a value of their own.
- **Loopback**: `DEBUG` and `A2A_ALLOW_INSECURE_LOOPBACK` exempt a
  plain-http request from real loopback only, never one carrying
  `X-Forwarded-For`: uvicorn rewrites the peer from that header for any proxy
  it trusts, so the header let a remote caller claim loopback. A plain-http
  proxy in front of a dev build no longer passes; call the backend directly.

`POST /runners/enroll` and `POST /mcp/oauth/token` only log a warning naming
this variable when they are reached over an unconfirmed transport, and keep
serving the request: they refuse it only under the opt-in
`CREDENTIAL_TRANSPORT_STRICT` (see *Opt-in: runner enrollment and MCP
tokens over confirmed HTTPS only* above), which needs this variable first.

Prefer this to `TRUST_PROXY_HEADERS` (see above), which honours the forwarding headers
from any peer and reads the caller-controlled start of the `X-Forwarded-For`
chain. Setting both leaves per-IP rate limiting on the weaker path, because
the application flag is consulted first. `FORWARDED_ALLOW_IPS=*` trusts any
peer and suits only a backend unreachable except through the proxy.

**The audit trail records the proxy's address** until the variable is set: it reads the client address from `X-Forwarded-For` only through a declared proxy, this variable or **Trust proxy headers** in Settings > Security (`TRUST_PROXY_HEADERS`). When an audited request carries that header from a peer the variable does not name - no proxy declared, or another one than the proxy the request came through - or names its client only in `X-Real-IP` or `Forwarded`, which the platform never reads, the platform logs one warning per process naming the peer that relayed it, and the security audit (Settings > Security) reports the warning "The audit trail records a proxy's address, not users'" (`network.client_addresses_undeclared_proxy`), naming that peer, while such a request has been seen on any replica in the last day. A deployment whose variable names another address than the one its proxy connects from, or whose proxy names the client only in `X-Real-IP` or `Forwarded`, therefore sees this warning where it saw a pass or an information finding, and the administrators' notification bell announces it once, as it does any new warning finding; no request is answered differently. Once the variable names the proxy, the warning stops and the finding becomes a pass; neither changes what is recorded. See [Configuration > Client addresses in the audit trail](docs/docs/deployment/configuration.md#client-addresses-in-the-audit-trail).

## Sharing an OAuth integration asks once, then shares its connected account

**No configuration change, no API change, no action.** In the UI, sharing an
OAuth integration - with a group, with individual users, or company-wide -
asks once, in a confirmation naming whose account it is and who will act
with it, and shares the connected account when you confirm (the
`Don't share my connected account` switch shares the integration without
it). Sharing an agent, a knowledge base, a skill or a flow that uses a
personal credential asks the same way. Nothing is refused.

- **API**: `keep_oauth_credentials_on_share` on `POST /api/v1/integrations`
  and `PUT /api/v1/integrations/{id}` keeps its meaning: `true` shares the
  connected account, `false` shares the integration without it, and a
  request that shares an account whose consent is not recorded without
  sending the field removes the token, as before. An edit that does not
  change sharing and sends no choice changes nothing.
- **Existing integrations** need nothing: shared integrations that still hold
  a token were acknowledged by the consent backfill (`b8ack2642001`).

In a chat, people who cannot see an integration still use their own account
instead. An agent, flow, knowledge base or skill that carries the
integration acts with it for its own audience; the integration's
`Sharing and access` card lists who that is. Each owner sees the
integrations sharing their connected account in the dashboard reminder, with a
shortcut to stop sharing.

## Durable memory writes now emit a flow event

**Applies to every deployment at rollout. No configuration change is
required. One migration runs**: it adds a single index to `flow_events` and
touches no column or row, so there is no table rewrite and no backfill — see
*One new index* below for its one failure mode. A builtin `Memory persisted`
event type is created at startup if it does not already exist — the same
create-if-absent seeding the runner event types use — and every durable agent
memory write now emits it.

**Nothing starts reacting on its own.** The event only reaches a flow that an
author explicitly binds to it with an `On event` node, so existing flows,
assignments and schedules are untouched. The same event name is also published
on the platform hook bus as `memory.persisted`; a deployment with a hook
registered for `*` (all events) will start receiving these deliveries, which is
the one behaviour change to be aware of before upgrading. Register the specific
event names instead if that is unwanted.

**Emission is fail-open and adds no write to the memory path**: the event row
is written inside the same transaction as the memory entry, and any failure
rolls back to just before it, leaving the memory write intact. Hook deliveries
are held until that transaction commits, so a hook never sees a memory whose
write was rolled back.

**Hook delivery is best-effort and at-most-once — it is not a durable
outbox.** The converse of the guarantee above does not hold: once the write
commits, the delivery is scheduled in-process with no retry and no persistence,
so a process terminated between the commit and the delivery drops that
`memory.persisted` notification silently. The memory itself is durable, and the
in-platform path (a flow bound to `Memory persisted`) is unaffected — it is
driven by the committed `flow_events` row, not by this notification. If an
external subscriber needs every write, reconcile against the memory API rather
than treating these deliveries as a complete log.

**`flow_events` now has a retention policy, and needs one.** That table was
append-only with no sweep; memory writes make it the fastest-growing
coordination table on a busy platform. Two mechanisms bound it:

- An entry's older emissions are deleted as it is rewritten, so a consolidated
  memory or an operational block moving through its statuses keeps one row
  rather than one per write.
- The hourly maintenance tick purges emissions older than
  `FLOW_EVENT_RETENTION_DAYS` (default **30**), oldest first, at most
  `FLOW_EVENT_SWEEP_BATCH` rows per pass (default 2000). Set the retention to
  `0` to disable the sweep and keep the previous unbounded behaviour.

The sweep never uses a window shorter than `FLOW_RUN_TIMEOUT_SECONDS`, whatever
the retention is set to: a parked `Wait for event` node polls for emissions
newer than its park, so a shorter window could delete the event a live run is
waiting for. A low retention value therefore keeps more than asked rather than
stranding runs.

**One new index**, `ix_flow_events_type_memory_id`, built `CONCURRENTLY` —
`flow_events` is written by every emission, so a plain build would block them
for its duration. If a deployment interrupts the migration mid-build, Postgres
leaves the index `INVALID` rather than absent and the re-run's
`IF NOT EXISTS` will not repair it; drop and re-create it when convenient:

```bash
psql "$DATABASE_URL" -c "DROP INDEX CONCURRENTLY IF EXISTS ix_flow_events_type_memory_id"
```

## New `PLATFORM_MODE=ai_disabled`

**Opt-in. No action needed unless you want it.** Existing deployments are
unaffected: the mode is additive and the new `platform_settings.ai_disabled`
column arrives `NULL`, which means "follow `PLATFORM_MODE`".

Set `PLATFORM_MODE=ai_disabled` to run the platform with AI off by
construction — nothing seeded, no AI schedulers, no LLM or embedding traffic,
and no provider-dependent containers needed to boot. Deterministic flows, their
triggers, integration syncs and all non-AI administration keep working, and AI
data stays browsable read-only.

**The runtime toggle.** `PLATFORM_MODE` sets the deployment default; an
administrator can override it from Settings → Parameters without a redeploy,
and set it back to *Deployment default* to follow `PLATFORM_MODE` again.
Turning AI on provisions the built-in content before the save returns, so the
first save after enabling takes a moment. On a cold or large install that can
outlast a short reverse-proxy timeout (30s is a common default) and the browser
shows a network error. **The setting has already been saved and provisioning
continues** — reload rather than retrying, and raise the proxy's read timeout if
you hit it repeatedly. Provisioning is idempotent, so a retry duplicates
nothing. Turning it off stops new dispatch
within one scheduler interval and refuses AI actions; runs already in progress
finish under their own time limit rather than being cancelled, so activity can
continue for a few minutes. Nothing is deleted either way.

## Flows and the chat context gauge now ship unflagged

**Applies to every deployment at rollout.** No configuration change is
required, and none is possible: the features below are no longer behind a
feature flag.

### What changed

`FLOWS_BUILDER_V2`, `FLOWS_EXECUTION_V2` and `CHAT_CONTEXT_USAGE` have been
removed from the flag registry, and the code paths they used to gate are now
always on:

* The **visual flow builder** — the `Flows` tab of `Agentic Flow`, the canvas
  at `/flows/library/:id`, and the `/api/v1/flows` + `/api/v1/flow-event-types`
  routes, which no longer answer `403`.
* **Flow execution** — activate/run/history/cancel, the cron tick, inbound
  webhooks and polled integration events, plus the run/activate controls in the
  builder toolbar.
* The **chat context gauge** — the composer's context-window readout, whose
  figures now always ride the SSE stream and the assistant message metadata.

### What operators need to do

Nothing, other than dropping the three ids from `ENABLED_FEATURES` if they are
set. An unknown flag id is logged as a warning and ignored, so leaving them in
place does not prevent startup — it just produces a warning on every boot.

Two visible consequences on a platform that never enabled them:

* The `Flows` tab becomes the default tab of `Agentic Flow`, replacing the
  agent-to-agent map.
* A built-in example flow and the three builtin runner event types are seeded
  at startup (create-if-absent, so a restart never duplicates them).

## Context windows now resolved from model metadata

**Applies to every deployment at rollout.**

### What changed

The agentic loop resolves a model's context window through a new chain: admin
per-model override -> bundled model-metadata snapshot (`max_input_tokens`) ->
model-family floor -> provider class default. It previously consulted only the
admin override and the provider class default.

That default is a single flat value per provider, and for the aggregating
providers it is deliberately optimistic: Anthropic, Bedrock, OpenRouter and
Gemini each declare `1_000_000` for every model they can reach. A Claude Sonnet
4.5 run (real window 200k) was therefore budgeted against five times its actual
capacity.

### Operational consequence: compaction fires earlier

The window is the denominator the loop budgets against, so correcting it moves
every threshold derived from it. On the affected providers, conversation
compaction now triggers at 80% of the model's real window instead of 80% of an
imaginary 1M one — much earlier in wall-clock terms, and much earlier than
before this release.

This is the intended fix. Previously the loop waited for headroom that did not
exist, and long conversations failed with a provider-side context-overflow
rejection instead of being summarised in time. Expect long threads to start
showing summarised earlier turns sooner than they used to.

### If a model really does have a larger window

Anthropic's 1M long-context beta is opt-in, so the bundled floors do not assume
it. If your deployment is enrolled, raise the window per model in
**Settings -> AI Models** (stored as `LLMProviderConfig.context_windows`). That
override takes precedence over both the metadata snapshot and the family
floors, and restores the later compaction point.

No action is required otherwise, and nothing errors if you take none.

## Alembic migration history squash

The entire Alembic migration history was collapsed into a single baseline
migration (`backend/alembic/versions/d046bcdf345f_baseline_schema.py`,
revision `d046bcdf345f`). The database **schema** is unchanged — only
Alembic's `alembic_version` bookkeeping row moves.

### Precondition: be fully upgraded first

Your database must have been **fully migrated on the last pre-squash release**
before you deploy the squashed version. The automatic transition moves the
version pointer only — it cannot replay migrations you skipped. If you are
several releases behind, upgrade to the last pre-squash release first (boot it
once so all migrations run), then deploy the squashed version.

### Normal case: no manual action

On first boot of the squashed version, startup detects a database stamped at
the last pre-squash head and re-stamps it to the baseline automatically
(`alembic stamp --purge`, metadata-only, zero DDL, zero data change). You will
see log lines like:

```
Database is stamped at the last pre-squash head '...' ... Auto-healing:
re-stamping to the baseline 'd046bcdf345f' with purge (metadata-only, ...)
Auto-heal complete - ... continuing startup normally.
```

Any migrations added after the squash remain pending and are applied by the
normal `upgrade head` that follows in the same boot.

### Failure case: unrecognised revision (fail-closed)

If `alembic_version` contains anything other than the last pre-squash head or
a known post-squash revision, the app **refuses to boot** instead of guessing.
This catches under-upgraded databases (an intermediate pre-squash revision), a
database newer than the image (see [Rolled back to an older image](#rolled-back-to-an-older-image)),
a corrupted marker, or a wrong-database mistake. The error names the case. For a
pre-squash revision, fix the cause first:

- **Under-upgraded database**: deploy the last pre-squash release, let it
  migrate to its head, then deploy the squashed version again.
- **Genuinely fully-upgraded database** (you verified the schema matches the
  last pre-squash release): re-stamp manually, then start the app:

  ```bash
  cd backend
  python -m alembic -c alembic/alembic.ini stamp --purge d046bcdf345f
  ```

  `--purge` is required — a plain `stamp` tries to resolve the current
  (now-deleted) revision first and fails. If you cannot run Alembic, the
  equivalent SQL is `UPDATE alembic_version SET version_num = 'd046bcdf345f';`.

### Rolled back to an older image

If the error says **the database is ahead of this image**, the database was
migrated by a newer build than the one now starting (a rollback, or an older
release tag deployed after a newer one ran its migrations). The schema is ahead
of the code, so the app refuses to boot and readiness stays 503.

Fix: deploy an image whose migration history contains the revision named in the
error (a build at or after the commit that added it). Do **not** re-stamp the
database: `stamp --purge d046bcdf345f` would hide the newer schema and replay
every post-squash migration on the next upgrade.

### Rolling back to a pre-squash release

The schema is identical on both sides, so rollback is a pointer move in the
opposite direction: stamp the database back to the revision the old code
expects (the last pre-squash head printed in the auto-heal log line), then
deploy the old version. Post-squash migrations that already ran must be
downgraded first (`alembic downgrade d046bcdf345f`) before re-stamping.

Developer background, verification methodology, and disaster recovery drills:
[`dev-docs/migration-squash-verification.md`](dev-docs/migration-squash-verification.md).
