# Integrations: OpenAPI / Swagger integrations and custom bundles

How an imported OpenAPI document becomes an integration, its invariants, the declarative executor it shares with custom integration bundles, and the bundle format; the rules live in `backend/app/integrations/AGENTS.md`.

## OpenAPI / Swagger integrations (`openapi`)

One catalog provider, `openapi`, for every imported API. A user creates it with
`POST /integrations` plus an `openapi` block (`url`, `content` = the file's
text, **or** `archive` = a base64 ZIP of a split spec; `selected_operations`,
`auth_option`, `tool_prefix`, `tool_overrides`); the spec is
converted once and stored on `IntegrationOpenAPISpec` (1:1 with the
integration, `ON DELETE CASCADE`). Nothing is generated as code.

An agent does the same in one call: the agent builder's
`create_integration` takes the same block as its `openapi` argument, and
`update_integration` takes it to replace the spec (`PUT /{id}/openapi`'s rules:
same credential type, tool overrides carried, OAuth endpoints followed). The
argument adds `file_id`, a conversation file whose bytes become `content` (a
ZIP becomes `archive`). `services/openapi_import/builder_tools.py` holds the
argument's schema and validation (the route's `OpenAPIImportOptions`) and calls
the routes' own helpers (`ensure_openapi_enabled`, `limit_spec_fetches`,
`plan_openapi_import`, `openapi_extra_config`, `replace_spec`), so the admin
switch, the per-user spec-load rate limit and every validation apply to the
tools as to the routes. The builder reads the catalog entry's `spec_import`
marker, never the provider name.

A run builds its tool list, the names the model calls and the gate at the
start of a turn. A spec, credential or sharing change written by
`update_integration` reloads the integration tools of the turn that made it
at the end of that step (`ToolDefinition.changes_tool_surface`, the loop's
refresh through `services/tool_surface_refresh.py`, see
`backend-tools-agent-loop.md` 3k); any other agent reads it from its next chat
turn or assignment run. The answer says so (`services/tool_surface_timing.py`,
`INTEGRATION_RELOAD_LINE`). A binding or tool-scope change written by
`update_agent` reaches the agent from its next chat turn or assignment run
(`NEXT_TURN_LINE`). A label-only change (name, description, tags, scopes)
carries no such line.

| Piece | Where | Role |
|-------|-------|------|
| Loader | `services/openapi_import/document.py` | JSON first, safe YAML second (keys and timestamps kept as literal text, alias bombs and non-JSON tags refused), 10 MB ceiling, URL fetch via `web_fetch.fetch_url_bytes(allow_private=True)` |
| Bundler | `services/openapi_import/bundle.py` | `bundle_external_refs()`: every document a spec references (relative / absolute URLs, ZIP members, `$self` base URIs) embedded under `x-bundled`, refs rewritten to local pointers, so the rest only resolves local refs; `read_spec_archive()` bounds and reads a ZIP |
| Discovery | `services/openapi_import/discovery.py` | `fetch_discovery()`: the OpenID Connect / RFC 8414 metadata that `openIdConnect` schemes and 3.2 `oauth2MetadataUrl` name, fetched with the spec |
| Schemas | `services/openapi_import/schema.py` | Local `$ref` resolution with cycle stubs; OpenAPI 2.0/3.0/3.1 schema → provider-safe tool JSON Schema (`allOf` merged, `oneOf` → `anyOf`, `readOnly` dropped, `pattern` described but never enforced, size-bounded) |
| Auth | `services/openapi_import/security.py` | Security schemes (every OAuth2 flow, OpenID Connect via discovery, mutual TLS) → `AuthOption`s → integration `auth_type` + the runtime `auth` block |
| Operations | `services/openapi_import/operations.py` | `analyze_spec()`: servers, operations → tools (parameters by location + style, 3.2 `querystring`, flattened / whole / form / multipart / text / sequential bodies, file arguments), every method but TRACE and CONNECT, names, skip reasons |
| Storage + runtime view | `services/openapi_import/service.py` | `prepare_import` / `apply_plan` / `update_selection`; `active_tools_for()` applies the user's tool names and descriptions; `effective_tools()` is THE runtime view (tool builder, `/tools` listing and the probe all use it) |
| Tool builder + probe | `providers/openapi.py` | Registers `active_tools` on the executor; connectivity probe = one argument-less, credential-carrying GET |
| API | `api/v1/integration_openapi.py` | `POST /openapi/preview`, `GET/PUT /{id}/openapi`, `PUT /{id}/openapi/operations`, `POST /{id}/openapi/refresh`, `PUT/DELETE /{id}/openapi/client-certificate`, `POST /{id}/openapi/device/poll`, `POST /openapi/oauth/implicit` |

Invariants worth keeping:

- **One provider key.** The admin kill switch, bindings and multi-instance
  suffixing all key on the provider; a per-API key would silently bypass
  them. Several OpenAPI integrations on one agent are just multi-instance
  `openapi` bindings.
- **Tool scopes are per integration.** Each OpenAPI integration exposes its
  own tools, so the agent/assignment tool-scope editor writes
  `integration_allowed_tools["openapi@<integration id>"]`
  (`filtering.instance_scope_key`, frontend `lib/tool-scope.ts`) instead of the
  provider key; `filter_integration_tools` applies a provider list and an
  instance list together, and `Agent.prune_orphaned_integration_scopes` drops
  an instance key once its integration is unbound. A provider-wide `openapi`
  list is read per integration at runtime (it restricts an integration whose
  tools it names, every one when empty), and an import folds the source
  install's instance keys into it (`filtering.fold_unbound_instance_scopes`),
  so an imported scope is never silently widened.
- **Tool names ≤ 48 chars** (`MAX_TOOL_NAME_CHARS`): multi-instance suffixing
  appends `__<slug>` (a 4 to 24 character slug, the integration's stored
  `tool_slug`, stable across renames) within the 64-character
  provider limit, so a 48-character name keeps a 14-character slug; an instance
  whose names leave no room is dropped.
- **Stored tools are auth-agnostic.** The credential's placement is applied at
  runtime from the stored auth option, and a parameter the credential fills
  (an `api_key` header declared both as scheme and parameter) is stripped
  from the arguments there — so the option can change without re-converting.
- **The auth type follows the spec.** `create_integration` derives `auth_type`
  from the chosen option; `update_integration` refuses a generic switch for
  `openapi`; `replace` keeps the same credential type.
- **Destination guard on every user-supplied URL**: spec download, referenced
  documents, discovery documents, tool calls (`enforce_destination_guard=True`)
  and every OAuth request (token grants, code exchange, refresh, device
  authorization — `providers/openapi.token_client_options`) — the
  operator-network policy of `app.core.destination_policy` (private networks
  allowed, loopback / link-local / reserved / cloud metadata refused), never a
  second address list. Connections are pinned to the checked address
  (`web_fetch.guarded_transport`), so a DNS answer that changes after the
  check cannot redirect them; behind an environment proxy,
  `web_fetch.guarded_client_options` checks every request's URL (each redirect
  hop included) before sending it, and a host `NO_PROXY` exempts is still
  connected to pinned. A refusal never names the address a host resolved to,
  and a fetch that reached nothing (refused, reset, filtered, unresolved) reads
  "could not be reached" whatever stopped it; spec loads are rate-limited per
  user and a URL preview is audited, loaded or not.
- **OAuth endpoints a spec names use HTTPS** (`security.is_secure_oauth_url`):
  an option on plain HTTP, or a discovery document served over it, is listed
  as unsupported. A refresh or a replace never moves an endpoint the
  integration holds to ANOTHER origin (`_follow_oauth_option` keeps it and the
  spec's warnings say so): the stored secret, a password or a device code
  would go to a server the user never chose. A device-code flow is bound to
  the token URL and client id it started with.
- **The credential never rides in a transport header** (`Host`,
  `Content-Length`, `Transfer-Encoding`, `Content-Type`...):
  `credential_header_allowed` refuses it at import and at call time
  (`Authorization` and `Cookie` stay allowed; mapped cookies are added to a
  cookie credential, never written over it, and a mapped cookie of a name the
  credential carries is not sent). A query object explodes into
  parameters only under a DECLARED `style` (the importer writes it), so a
  bundle mapping always sends under its declared name.
- **A path value stays in its segment** (`declarative_http_executor._path_segment`,
  OpenAPI and bundle tools alike): every reserved character is
  percent-encoded, and a value is refused before any request when it is
  empty or holds a `.` / `..` segment as sent or after up to 8 rounds of
  percent-decoding, split on `/` and `\`, a `;` parameter cut off
  (`%252e%252e`, `a%2F..%2Fb`, `..;x`), or when it still decodes past that
  bound: a proxy and an application that each decode the path once
  would otherwise normalise the credentialed call onto an endpoint the spec
  never declared. `v1..2` is sent.
- **Sign-in flows.** `GET /{id}/authorize` starts what the stored option says:
  a code redirect (PKCE), an implicit redirect (`response_type=token`; the
  callback sends a query-less redirect to the frontend's `/oauth/implicit`,
  which posts the fragment back with its single-use state), or a device code
  (`{"device_authorization": …}`; the device authorization endpoint is stored
  in `authorize_url`). Implicit and password flows are never the default
  option while another one works.
- **Client certificates** are presented by every connection an integration
  makes, tools and OAuth requests alike, whatever the auth option; they never
  reach a request (the executor gets `extra_config` without them). The mutual
  TLS option (`auth_type` `none`) requires one and refuses its removal. An
  expired one is refused when pasted and when used (`client_ssl_context`
  raises, so tools answer with the reason); within `EXPIRY_WARNING_DAYS` it is
  `expires_soon` for the UI and logged once a day per integration.
- **The credential stays on the base URL's origin.** An operation whose own
  `servers` entry is on another scheme, host or port is called without it
  (`declarative_http_executor.same_origin`), and the preview warns about it.
- **Imported `pattern`s are never enforced.** Tool arguments are validated
  with Python's backtracking `re` before dispatch, so a spec's regex could
  pin a worker; the converter writes it into the description instead.
- **Refresh never widens the surface**: new operations stay off until selected.
- **File arguments come from the conversation.** A multipart file field, or a
  body in any media type that is not JSON / form / text, becomes a file
  argument (`file_argument_schema`): `file_id` of a file in the run's
  `FileContext`, which the tool runtime injects into the handler as
  `file_context` (a reserved argument name), or `content` text. The executor
  reads it (`_resolve_file_arguments`), caps it at `MAX_UPLOAD_BYTES`, and
  sanitises the filename and media type.
- **User tool names and descriptions.** `tool_overrides` (operation key →
  `name` / `description`) applies on top of `tool_definitions` in
  `active_tools_for`, is validated by `clean_tool_overrides` (≤ 48 chars, no
  `__`, not a built-in tool's name, unique among the selected tools), and is
  kept by refresh and replace (`carry_tool_overrides`).
- **The API key is optional.** An `api_key` OpenAPI integration without a key
  is `connected`, and `_get_token` hands it the no-auth marker, so its calls
  carry no credential until a key is stored.
- **No per-user (template) binding.** "The user's own `openapi` integration"
  names no API in particular, so `PER_USER_UNRESOLVABLE_PROVIDERS`
  (`openapi_import/service.py`) keeps the provider out of
  `template_candidate_clause`, out of the shadowed-binding fallback, out of
  `/per-user-readiness`, and refuses it in the agent create/update schemas and
  the agent builder. The frontend mirrors it with `resolvesPerUser()`
  (`lib/utils.ts`). A shared agent's members get an OpenAPI integration's tools
  only when the integration itself is shared with them. For each member to
  act with their own account on a shared OpenAPI integration, the integration
  takes `credential_mode = "per_user"` (below): one spec and one OAuth
  app, one account per person.

The declarative executor (`services/declarative_http_executor.py`) is shared
with custom bundles, so every capability OpenAPI needed is available to
bundle authors too: `path_mapping` (argument ≠ placeholder), `header_mapping`
and `cookie_mapping` (reserved / credential headers unmappable), query
`style` / `explode` (`form`, `spaceDelimited`, `pipeDelimited`,
`tabDelimited`, `deepObject`, `json`), `querystring` (the whole query string
as one argument), `body_from` + `body_required` (the whole
body is one argument), `body_encoding` (`json` / `form` / `multipart` fields
and files / `text` / `binary` / `jsonl` / `json-seq`), file arguments
(`"file": true` multipart fields and `binary` bodies, read from the
conversation's `FileContext`), any HTTP method but TRACE and CONNECT (HEAD
and OPTIONS answer with the status and headers), `accept`, `server_url`,
`ssl_context` (a client certificate), and `auth.type` `query` / `cookie` / `basic`. Handlers are built by `make_declarative_handler()` only and return a
`ToolResult` flagged with `is_error_shaped_result`, so a failed call is
recorded as a failure everywhere (trace, counters, tool test, probe).
An answer reaches the model only as text (`_answer_is_text`): a
declared text, JSON or XML type (`file_content.TEXT_MIME_PREFIXES`, `+json`,
`+xml`) is read as before, any other type or none only when its bytes are
strict UTF-8 holding no NUL. A binary answer, and JSON nested past the
recursion limit, return one fixed `Error:` sentence, the reason in the logs
only. An `untrusted_response` call sends exactly one
`Accept-Encoding: identity`, whatever case a header mapping or the credential
spelled that name in.

OAuth2 client credentials (machine to machine) and the password grant use
`integrations/oauth_token_grants.py`: one cached, coalesced token per
configuration, Basic client authentication with a single fallback to
credentials in the body, and the token URL behind the destination guard. Imported providers
and OpenAPI integrations own their OAuth scopes
(`integration_catalog.integration_owns_oauth_scopes`), so the authorize step and
the callback never overwrite them from the catalog.

Every declarative answer is read bounded. A tool without
`untrusted_response` keeps its retries, its 401/403 refresh, the destination
guard with its pinned transport and the client certificate; its client gets a
response hook (`_bounded_answer_hook`) that runs on each attempt before httpx
reads the body. The request carries exactly one `Accept-Encoding: identity`
(a mapped one, in any case, is replaced), a compressed answer is refused at
its first byte, and raw bytes are counted up to `max_response_bytes`, by
default the 20 MB cap: a declared `Content-Length` over it is refused unread,
a streamed body stops at the first chunk past it. Each refusal is one fixed
`Error:` sentence, the same as on the `untrusted_response` path. A JSON
answer keeps its exact shape (flows parse it), a HEAD or empty OPTIONS answer
still gives its status and headers.

## Custom integration bundles (`filigran-integration-v1`)

An admin imports a provider from a ZIP (`metadata.json` + `checksum.sha256` + assets) through `POST /integrations/admin/import`, parsed by `services/integration_bundle_manager.py`; it becomes `custom:<slug>`. A declared tool with `execution` is run by `services/declarative_http_executor.py`: one HTTP call to `{extra_config[base_url_key]}{path}`, its body / query mapped from the tool arguments (`"arg"`, `{"from": "arg", "required": true}`) or fixed by the bundle (`{"value": ...}`, a scalar in a query string) - a constant is never read from the arguments, its `auth` (and the Test button's `test_connection.auth`, validated by the same `validate_auth_spec`) `bearer`, `header` or `none`; `"untrusted_response": true` (with an optional `max_response_bytes`, capped) streams the answer as raw bytes within that budget, without redirects or proxy settings, refuses a compressed one, answers an error status with a fixed sentence and hands the result to the model inside `wrap_untrusted` (the XTM Hub bundle sets it on every tool), so a GraphQL tool carrying its own `query` exposes that operation only. `scripts/build_xtm_hub_bundle.py` builds the XTM Hub bundle from the native provider's query fields. An OAuth bundle whose service redeems the authorization code itself declares `oauth_exchange` (`service_token_exchange.py`): the callback hands the code to `{base_url}{code_path}` instead of exchanging it at `token_url`, a refresh posts the refresh token to `{base_url}{refresh_path}`, and both answers are mapped to `access_token` / `refresh_token` / `expires_in` (JWT `exp`), so the scheduled and inline refreshes keep their lock, cooldowns and verdicts. An OAuth bundle's `oauth` block (`authorize_url`, `token_url`, `scopes`; `integration_bundle_manager.oauth_endpoint_errors`) is what its catalog entry offers, so an integration created from the catalog form - bundle settings plus client ID / secret - has its endpoints; a URL spelling `/common/` takes the integration's `tenant_id` setting, as Entra ID providers do (`oauth._resolve_tenant_urls`). A custom provider keeps its stored scopes across authorizations (no catalog to sync them from).

A bundle integration's base URLs (`extra_config.base_url` and every `base_url_key` its tools name, `base_url_policy.base_url_keys`) are user-supplied: anyone who sees the provider may configure an integration of it. `integrations/base_url_policy.py` holds them to the OpenAPI integrations' operator-network policy (private networks allowed; loopback, link-local, reserved and cloud metadata refused): on save (`assert_base_url_allowed`, a 422 through `helpers.require_allowed_base_urls`, on create, update and an imported `integration_instance`; every address the host resolves to now, an unresolved name accepted; a relay-routed integration judged only on a spelled metadata address), on every tool call (`integration_tools._make_declarative_handler` passes `enforce_destination_guard=True`, with `spec_servers_own=True` so a bundle's own `server_url` keeps the credential without a configured base URL), and on the `test_connection` probe (`probe_transport()`, pinned, no CA or proxy from the environment). An `untrusted_response` call keeps only the guard's pinned transport, never its proxy mounts.
