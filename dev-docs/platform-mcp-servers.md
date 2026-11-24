# Platform MCP Servers (OpenCTI / OpenAEV / OpenCRQ)

XTM One embeds a native **Model Context Protocol (MCP) server** for every
connected Filigran platform. Any MCP-compatible client (Cursor, Claude
Desktop, Claude Code, custom agents, ...) can connect directly to OpenCTI,
OpenAEV, and OpenCRQ **through XTM One** and work with threat-intelligence
knowledge, adversarial-exposure-validation content, and
governance-risk-compliance data.

This replaces the earlier standalone attempts (the archived `xtm-mcp`
repository and the OpenCTI in-platform MCP prototypes): the MCP servers now
live in XTM One, reuse the battle-tested native integration tooling, and are
maintained together with it.

## Endpoints

| Endpoint | Platform | Content |
| --- | --- | --- |
| `POST /mcp/opencti` | OpenCTI | STIX knowledge: SDOs, SCOs (observables), relationships, indicators, containers (reports/cases/groupings), notes, labels, markings, stats |
| `POST /mcp/openaev` | OpenAEV | Scenarios, simulations (exercises), payloads, injects, atomic tests, expectations, findings, vulnerabilities, assets, media content, lessons learned, reports |
| `POST /mcp/opencrq` | OpenCRQ | Compliance frameworks (assessment catalog and Drata mirror), requirements, control capabilities (assessment + history), compliance assessments (answers, publish), risk scenarios, attack outcomes, CRQ (simulations, what-if, TEF, loss drivers/estimates), assets (lifecycle, business value categories, dependency roles, evidence), unique findings, findings and posture, threats, treatment plans, budget reviews, proven control posture, risk dashboard, MITRE ATT&CK, CVEs, job tracking |

All endpoints implement the MCP **Streamable HTTP** transport (JSON-RPC
2.0 over POST, stateless), shared with `/mcp/user`, `/mcp/global` and
`/mcp/xtm` through `backend/app/api/v1/mcp_transport.py`.
Supported methods: `server/discover`, `tools/list`, `tools/call`, and per era
`initialize` / `ping` (handshake revisions) or `subscriptions/listen`
(2026-07-28). A notification or a client's response (a message without an id)
is never dispatched and gets no answer (`202`, no body), in either era; a
`notifications/*` method sent with an id is a request and is answered
`-32601`. `jsonrpc` must be `"2.0"` (`-32600` otherwise). SSE and stdio
transports are not supported: `GET` and `DELETE` are `405` with `Allow: POST`.

- **Protocol revisions**: `2026-07-28` and the handshake revisions
  `2025-11-25`, `2025-06-18` and `2025-03-26`. The servers are dual-era:
  - **2026-07-28** (no handshake): a request whose `params._meta` names a
    protocol version, or whose `MCP-Protocol-Version` header names one that
    is not a handshake revision, is served statelessly. It must carry
    `io.modelcontextprotocol/protocolVersion` and
    `io.modelcontextprotocol/clientCapabilities` (else `400`, `-32602`), and
    the `MCP-Protocol-Version`, `Mcp-Method` and, for `tools/call`, `Mcp-Name`
    headers must agree with the body (Base64 sentinel decoded; else `400`
    `HeaderMismatch`, `-32020`). Every result carries `resultType` and the
    server's `io.modelcontextprotocol/serverInfo` in `_meta`; `tools/list`
    adds `ttlMs` (60 s) and `cacheScope: private` (the list depends on the
    caller). `subscriptions/listen` is acknowledged with no notification type
    (these servers emit none) and closed gracefully over SSE; `ping`,
    `initialize` and `logging/setLevel` are unknown methods there (`404`).
    Such a request needs an audience-bound OAuth token: a token without `aud`
    is refused (`401`), while a handshake-era request keeps the old
    acceptance.
  - **Handshake revisions**: `initialize` echoes a supported legacy
    `protocolVersion` and answers `2025-11-25` otherwise, `2026-07-28`
    included; `serverInfo` carries `name` and `version`, plus `title` from
    `2025-06-18` and `description` from `2025-11-25` (each only to a client
    that negotiated a revision defining it).
  - `server/discover` is answered in both eras: every supported version,
    `capabilities: {tools: {listChanged: false}}`, the server's identity,
    `ttlMs` 1 h, `cacheScope: public`.
- **`MCP-Protocol-Version`**: an unsupported value is a `400`
  `UnsupportedProtocolVersionError` (`-32022`, `data.supported` /
  `data.requested`), except on a handshake `initialize`, where the revision is
  negotiated; no header means `2025-03-26`, as the specification says. Any
  supported revision is accepted, so a `2026-07-28` notification or client
  response is a `202` and a `2026-07-28` batch is refused as a batch.
- **Batches**: a JSON array of messages is accepted under `2025-03-26` (at most
  16 messages, of which at most 4 `tools/call`, and never an `initialize`) and
  refused with a `400` under the later revisions, which removed batching. The
  answers form an array the client matches by `id` (invalid elements first);
  an element whose handling fails is answered `-32603` and the others keep
  their answers.
- **`Origin`**: a web origin other than the platform's own (`BASE_URL`,
  `FRONTEND_URL`) or one of `CORS_ORIGINS` - `null` included - is a `403`,
  checked before authentication (DNS rebinding); `CORS_ORIGINS=*` admits
  every origin, as it does for CORS. No `Origin`, or a non-web
  one (a browser extension, an application scheme), is not checked. A refusal
  is logged at `INFO` with the origin; a configured origin that cannot be read
  is logged once at `WARNING`.

## Availability

A platform MCP server is live **as soon as the platform is registered**
with XTM One (`POST /api/v1/platform/register` — the same registration that
powers the embedded "Ask AI" chat). No additional configuration is needed:

- Registration auto-creates the company-managed `platform_managed`
  integration used for tool execution.
- While no platform of that type is registered (or the registration was
  deactivated), the endpoint returns `404`.
- This is a **full Community Edition** feature. Unlike the agent MCP
  endpoints (`/mcp/user`, `/mcp/global`), there is no chat/EE license gate:
  tool calls go straight to the platform APIs without any LLM involvement.

Admins can disable all platform servers globally with the **Platform MCP
Servers** toggle in *Settings > MCP Endpoint*
(`PlatformSettings.platform_mcp_enabled`, default on). When disabled, the
endpoints return `403`.

## Authentication and identity

Requests are authenticated with a bearer token, exactly like `/mcp/user`:

- a personal API key (`fcp-...`, created in *Profile > API Keys*), or
- an OAuth 2.1 access token (RFC 9728 discovery: an unauthenticated request
  receives `401` with a `WWW-Authenticate` header pointing at
  `/.well-known/oauth-protected-resource/mcp/{platform}`). The client
  registers at `POST /mcp/oauth/register` and obtains the token at
  `POST /mcp/oauth/token`; under `CREDENTIAL_TRANSPORT_STRICT` both refuse a
  request the platform cannot confirm arrived over HTTPS (`400`, error
  `invalid_client_metadata` and `invalid_request`, description
  `https required`) before reading it or issuing anything.
- a client that does not register: the authorization server
  advertises `client_id_metadata_document_supported` (MCP 2025-11-25), and a
  client naming an HTTPS metadata document as its `client_id` (claude.ai,
  Claude Code, VS Code) is resolved by `backend/app/services/mcp_oauth_clients.py`:
  its host resolved once, every address public (the outbound URL policy,
  NAT64, 6to4 and Teredo addresses judged by the IPv4 they carry), and the connection made to that
  validated address with TLS verified against the host name (no DNS
  rebinding), no proxy, no redirect, at most 16 KiB of raw `application/json`
  in 5 s in all. A cache miss spends 20 fetches per client IP, 10 per host
  and 60 for the platform per minute, refused while Redis cannot count them;
  a document is fresh for its `max-age` (5 min to 1 h) and a known client is
  then served from its entry for 24 h more whenever a refetch is refused a
  budget or cannot reach the document. It is accepted only as a public client
  with a printable ASCII URL, a `client_name` (MCP 2025-11-25; no control or
  formatting character) and redirect URIs that are HTTPS or loopback, and
  the request must be able to succeed (supported PKCE method, a redirect URI
  that may receive a code) before anything is fetched. Every refusal is
  answered `Invalid client metadata document`; why is logged, never answered
  (it would reveal what the platform resolves a host to, or whether an
  address answered). It leaves no row in `mcp_oauth_clients` and spends no
  registration budget. The consent page names it by the host its document is
  served from, shows its `client_name` as what it says it is, and says which
  host the code goes to (flagged when it is this computer or another site).
  `MCP_CIMD_ENABLED` (Settings > Security) turns it off, which also stops its
  refreshes; a client whose document was refused since gets no new token, and
  the refresh tokens of one authorization stop rotating after 90 days. An
  `http` loopback redirect URI registered without a port matches whatever
  port is requested (RFC 8252 §7.3); one that pinned a port, or an `https`
  one, matches exactly. `MCP_DCR_RATE_LIMIT` sizes the per-IP registration
  budget (5 per hour by default).
- every authorization response names its issuer (`iss`, RFC 9207, advertised
  by `authorization_response_iss_parameter_supported`), against
  mix-up attacks; a registration may carry `application_type` (`web` /
  `native`), which is echoed in the registration response but not stored,
  and changes nothing (one redirect rule for both).
- a successful password sign-in on the consent page answers `200` with a
  page that sends the browser to the `redirect_uri` by a `meta refresh`
  (`_grant_response()` in `mcp_oauth.py`, `no-store`), not a `302`, since
  Browsers check `form-action` on every redirect a form submission
  follows, and a client's callback often redirects again. A client that
  drives the browser and reads the `Location` of that POST (a webview
  intercepting redirects, a headless harness) must follow the page instead;
  every grant answers through that one helper.

At tool-execution time XTM One mints a **short-lived Ed25519 JWT for the
calling user** (the same cross-platform JWT used by the embedded chat,
verified by the platform against `/xtm/auth/jwks`). Every OpenCTI / OpenAEV
/ OpenCRQ API call therefore runs **as the calling user** — matched by email
on the platform side — with that user's permissions, markings, and audit
trail. The platform's API token is never stored in or proxied through XTM
One.

## Tool surface

Tools map 1:1 to the native platform integration tools, filtered
through an explicit **fail-closed allow-list**
(`backend/app/api/v1/mcp_platform.py`): only business / knowledge tools are
exposed. Platform-orchestration tooling — users, groups, roles, playbooks,
dashboards, notifications, customization, audit, PIRs, ingestion, data
sharing, connectors, settings — is never exposed over MCP. A tool added to
the integration providers later stays hidden until explicitly allow-listed.

A listed description and a tool result name another tool only when the
server serves it: `tools/list` resolves each description against the
served tools (`mcp_tool_metadata.tools_to_mcp()`) and `tools/call` binds them
as the offered surface (`offering_tools()`). `get_opencti_container_full`
therefore lists a container's attached files with their IDs and download
links, without pointing at `download_opencti_file`, which the server does not
serve. `tests/test_mcp_tool_cross_references.py` checks every listed
description of the three servers.

Every listed tool carries a `title` and the MCP tool `annotations`
(`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`),
read from its name by `backend/app/services/mcp_tool_metadata.py` through the
platform's one read/write classifier (`services/tool_classification.py`):
a read is read-only (but a read found on a word of the name, not its verb,
never outweighs a destructive token: `intune_status_purge` is destructive), `create_*` / `add_*` / `run_*` are additive writes,
`update_*` / `delete_*` / `remove_*` / `manage_*` are destructive, and a tool
that starts an agent run (`trigger_assignment`) is annotated like an agent.
The platform servers act on one platform's data, so `openWorldHint` is `false`.
`tests/test_mcp_tool_metadata.py` fails on an allow-listed tool whose name the
rules cannot read: allow-listing a tool with a new verb means teaching that
module the verb. The same serializer lists the tools of `/mcp/xtm`; an agent
exposed on `/mcp/user` / `/mcp/global` is annotated as an agent run (not
read-only, destructive, open world) and its tool name is shortened to the 64
characters hosted clients accept when its slug is longer (`agent_tool_name()`: the slug's head, `-X` and a
64-bit digest, which no lowercase slug can spell; `tools/call` still accepts
the full slug).

### OpenCTI (28 tools)

| Category | Tools |
| --- | --- |
| Search & read | `search_opencti`, `get_opencti_entity`, `get_opencti_relationship`, `list_opencti_entities`, `list_opencti_observables`, `list_opencti_relationships`, `get_opencti_container_full`, `get_opencti_entity_stats`, `get_opencti_top_related`, `get_opencti_entity_history`, `validate_nlq_filter` |
| Create | `create_opencti_entity`, `create_opencti_relationship`, `create_opencti_observable`, `create_opencti_indicator`, `create_opencti_container`, `create_opencti_note` |
| Update | `update_opencti_field`, `update_opencti_relationship_field` |
| Delete | `delete_opencti_entity`, `delete_opencti_relationship` |
| Labels & markings | `add_opencti_label`, `remove_opencti_label`, `add_opencti_marking`, `remove_opencti_marking`, `list_opencti_markings` |
| Containers | `add_opencti_to_container`, `remove_opencti_from_container` |
| Advanced | `promote_opencti_observable` |

OpenCTI mutations run in **direct mode**: they write straight to the live
knowledge graph with the calling user's permissions. The conversational
draft-staging workflow used by XTM One agents does not apply to the MCP
server — the transport is stateless (a draft activated by one JSON-RPC
request cannot persist to the next) and MCP clients drive their own review
workflows. Draft management tools are therefore not exposed.

Excluded (platform orchestration): playbooks, workspaces/dashboards,
users/groups/roles, notifications, customization, audit, PIR management and
queries, ingestion feeds, data sharing, connector management, file
upload/download (depends on the chat file context), dev tools.

### OpenAEV (51 tools)

| Category | Tools |
| --- | --- |
| Search & read | `search_openaev_assets`, `get_openaev_asset`, `search_openaev_asset_groups`, `get_openaev_asset_group`, `search_openaev_teams`, `get_openaev_team`, `search_openaev_players`, `search_openaev_attack_patterns`, `get_openaev_attack_pattern`, `search_openaev_scenarios`, `get_openaev_scenario`, `search_openaev_exercises`, `get_openaev_exercise`, `search_openaev_payloads`, `get_openaev_payload`, `search_openaev_injector_contracts`, `get_openaev_injector_contract`, `search_openaev_findings`, `get_openaev_vulnerability`, `full_text_search_openaev`, `list_openaev_kill_chain_phases`, `list_openaev_tags`, `list_openaev_domains` |
| Scenarios & simulations | `create_openaev_scenario`, `update_openaev_scenario`, `delete_openaev_scenario`, `manage_openaev_scenario`, `manage_openaev_simulation`, `add_openaev_inject_to_scenario`, `manage_openaev_inject`, `manage_openaev_variable` |
| Expectations (security verdicts) | `manage_openaev_expectation` |
| Testing & attack content | `create_openaev_payload`, `manage_openaev_payload`, `manage_openaev_threat_arsenal`, `create_openaev_atomic_testing`, `manage_openaev_atomic_testing` |
| Findings & vulnerabilities | `manage_openaev_finding`, `manage_openaev_vulnerability` |
| Assets | `manage_openaev_asset`, `manage_openaev_asset_group` |
| Content | `manage_openaev_document`, `manage_openaev_channel`, `manage_openaev_article`, `manage_openaev_challenge` |
| Phishing components | `manage_openaev_phishing` (landing pages + email templates) |
| Lessons & reporting | `manage_openaev_lessons`, `manage_openaev_reporting` |
| Analytics | `query_openaev_analytics`, `manage_openaev_dashboard` |
| Preview features | `query_openaev_attack_path` (`ATTACK_PATH`), `query_openaev_chaining` (`INJECT_CHAINING`) |

Note on the preview tools: their endpoints answer **404 when the feature flag
is off**, which is indistinguishable from a wrong URL. Both tools check the
platform's `enabled_dev_features` first and return an explanation instead, so
an MCP client never has to guess.

Excluded (platform orchestration / people management): organizations,
users, groups, roles, profile/token management, team and player write
managers (`manage_openaev_team`, `manage_openaev_player` — reads stay),
security platforms, notifications (`manage_openaev_notification` — a
per-user inbox and delivery configuration, not shared knowledge), taxonomy
write managers (tags, kill chain phases, attack patterns), connectors,
catalog, platform settings.

### OpenCRQ (85 tools)

| Category | Tools |
| --- | --- |
| Tenancy & catalog | `list_opencrq_tenants`, `list_opencrq_organizations`, `list_opencrq_frameworks`, `get_opencrq_framework`, `list_opencrq_framework_requirements`, `list_opencrq_drata_controls` |
| Control capabilities | `list_opencrq_control_capabilities`, `get_opencrq_control_capability`, `update_opencrq_control_capability_assessment`, `manage_opencrq_scope_control_posture`, `manage_opencrq_capability_techniques`, `get_opencrq_control_capability_history` |
| Compliance assessments | `list_opencrq_compliance_assessments`, `get_opencrq_compliance_assessment`, `create_opencrq_compliance_assessment`, `manage_opencrq_compliance_assessment`, `update_opencrq_requirement_answer` |
| Risk & attack outcomes | `list_opencrq_risk_scenarios`, `get_opencrq_risk_scenario`, `create_opencrq_risk_scenario`, `update_opencrq_risk_scenario`, `delete_opencrq_risk_scenarios`, `update_opencrq_risk_tolerance`, `get_opencrq_scenario_exposure`, `get_opencrq_cve_exploitation`, `list_opencrq_attack_outcomes`, `get_opencrq_attack_outcome` |
| CRQ | `get_opencrq_financial_summary`, `run_opencrq_crq_simulation`, `get_opencrq_crq_run`, `list_opencrq_crq_run_history`, `get_opencrq_crq_what_if`, `get_opencrq_tef_input`, `update_opencrq_tef_input`, `list_opencrq_loss_drivers`, `get_opencrq_loss_driver`, `update_opencrq_loss_estimates`, `get_opencrq_financial_profile`, `update_opencrq_financial_profile` |
| Assets & vulnerabilities | `list_opencrq_primary_assets`, `create_opencrq_primary_asset`, `update_opencrq_primary_asset`, `delete_opencrq_primary_assets`, `list_opencrq_business_value_categories`, `list_opencrq_supporting_assets`, `get_opencrq_supporting_asset`, `create_opencrq_supporting_asset`, `update_opencrq_supporting_asset`, `delete_opencrq_supporting_assets`, `reset_opencrq_supporting_asset_to_connectors`, `list_opencrq_asset_dependencies`, `create_opencrq_asset_dependency`, `update_opencrq_asset_dependency_roles`, `delete_opencrq_asset_dependencies`, `create_opencrq_asset_evidence`, `update_opencrq_evidence_state`, `delete_opencrq_asset_evidence`, `reset_opencrq_evidence_state`, `get_opencrq_evidence`, `list_opencrq_cve_evidences`, `list_opencrq_unique_findings`, `search_opencrq_cves`, `list_opencrq_cves`, `get_opencrq_cve` |
| MITRE ATT&CK | `list_opencrq_mitre_tactics`, `search_opencrq_mitre_techniques`, `get_opencrq_mitre_technique`, `list_opencrq_mitre_mitigations` |
| Job tracking (read) | `list_opencrq_jobs`, `get_opencrq_job` |
| Findings & posture (read) | `get_opencrq_findings_summary`, `get_opencrq_top_vulnerabilities`, `list_opencrq_findings`, `get_opencrq_cve_finding`, `get_opencrq_asset_posture`, `get_opencrq_technique_test_posture`, `get_opencrq_control_validation` |
| Threats (read) | `list_opencrq_threats`, `get_opencrq_threat`, `list_opencrq_scenario_threats`, `list_opencrq_cve_threats` |
| Risk treatment & dashboard (read) | `list_opencrq_treatment_plans`, `get_opencrq_treatment_plan`, `list_opencrq_budget_reviews`, `get_opencrq_budget_review`, `get_opencrq_risk_dashboard` |

OpenCRQ is multi-tenant: every tool accepts an optional `tenant_id` and
resolves the caller's sole tenant automatically when omitted.

The list above is the full set. `tool_count` of `GET /platform/mcp-servers` stays
static (the length of the allow-list), but what `/mcp/opencrq` serves follows the
connected OpenCRQ instance: the tool build withholds the tools that instance
cannot serve, see `reference/integrations-and-knowledge-bases.md` ("OpenCRQ tools
follow each instance").

Excluded (platform orchestration / people management): tenant and user
administration (users, tenant users, groups, roles, capabilities,
invitations, tenant/organization lifecycle), service accounts, engine
settings tools - both reads and writes (CRQ / threat
relevance settings), job
cancel/retry, integration/connector configuration and status
(`list_opencrq_connectors`, `list_opencrq_connector_syncs`,
`configure_opencrq_connector`, `run_opencrq_connector`), platform
settings.
These tools remain available to agents through the regular OpenCRQ
integration.

## Client configuration

Users find their endpoints, connection status, and ready-to-copy snippets
in *Profile > MCP Endpoint > Platform MCP Servers*.

Cursor (`.cursor/mcp.json`) / Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "opencti": {
      "url": "https://your-xtm-one.example.com/mcp/opencti",
      "headers": {
        "Authorization": "Bearer fcp-..."
      }
    },
    "openaev": {
      "url": "https://your-xtm-one.example.com/mcp/openaev",
      "headers": {
        "Authorization": "Bearer fcp-..."
      }
    }
  }
}
```

Smoke test with curl:

```bash
curl -s -X POST https://your-xtm-one.example.com/mcp/opencti \
  -H "Authorization: Bearer fcp-..." \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'
```

## Error model

| Condition | Response |
| --- | --- |
| Web origin the platform does not serve | `403` |
| Missing / invalid token | `401` + `WWW-Authenticate` (RFC 9728) |
| Admin toggle off | `403` |
| Platform not registered | `404` |
| Unsupported protocol version | `400` + `-32022` with the supported versions |
| 2026-07-28 request missing a required `_meta` field / header disagreeing with the body | `400` + `-32602` / `-32020` |
| Body that is not JSON, invalid request, batch outside `2025-03-26` | `400` + JSON-RPC error (`-32600` / `-32700`) |
| Unknown method | JSON-RPC `-32601` (`404` for a 2026-07-28 request) |
| Unknown / non-allow-listed tool | JSON-RPC `-32602` |
| Arguments that are not an object | JSON-RPC result with `isError: true` (a tool execution error the model can correct, SEP-1303) |
| Tool executed but failed (platform error, permissions, ...) | JSON-RPC result with `isError: true` and the actionable message in `content` |

## UI descriptor endpoint

`GET /api/v1/platform/mcp-servers` (any authenticated user) is the single
source of truth for every UI surface: it returns the global
`platform_mcp_enabled` toggle plus one descriptor per server (`provider`,
`name`, `description`, `url`, `tool_count`, `connected`, `healthy`,
`platform_title`, `platform_version`). The shared
`PlatformMCPServerList` component (+ `usePlatformMCPServerList()` hook,
`frontend/src/components/ui/platform-mcp-server-list.tsx`) renders it in
both Profile > MCP Endpoint and Settings > MCP Endpoint with mode-aware
visibility: XTM One mode lists all servers (unconnected ones dimmed with a
registration hint), Copilot mode lists connected servers only and hides the
card entirely when none is connected.

## Implementation notes

- Endpoint module: `backend/app/api/v1/mcp_platform.py` (mounted at `/mcp`
  in `main.py`, tag `mcp-platform`). Shares the JSON-RPC transport with the other MCP endpoints
  (`mcp_transport.py`) and authenticates through `mcp_auth.py`, so no Platform MCP Server file
  imports the Enterprise Edition `mcp_endpoint.py` (pinned by
  `test_no_platform_mcp_server_file_imports_mcp_endpoint`). The rule is on direct imports only:
  the modules they build on may still reach Enterprise Edition code.
- Tool building delegates to `build_integration_tools()` with
  `context={"source": "mcp_platform", "user_id": ..., "direct_mutations": True}`;
  the OpenCTI provider skips the draft gate when `direct_mutations` is set,
  or when the integration's opt-in `live_writes` extra field is `on` (the
  builder kwarg `live_writes`, read in `integration_tools.py`; #11842). With
  `live_writes` off (the default) `apply_opencti_draft` and
  `create_opencti_label` are not registered and no tool text names the setting,
  on every path that builds OpenCTI tools; the MCP server's `direct_mutations`
  is unchanged.
- Execution is wrapped in a `ScopedToolExecutor` restricted to the
  allow-listed definitions, so a non-listed tool can never execute even if a
  client guesses its registered name (fail closed at both listing and
  execution time).
- Tool calls bump `call_count` / `last_called_at` on the platform-managed
  integration (visible on the integration detail page).
