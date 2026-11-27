# API routes in detail

Every route prefix of `backend/app/api/` with its file, purpose and auth, then the endpoints of each API (the rules live in `backend/app/api/AGENTS.md`; "Adding a security switch or check" is in `dev-docs/reference/backend-api-security-controls.md`, the prefix table in `dev-docs/reference/api-routes.md`).

## Existing Routes

One row per router `app/main.py` registers (`File` is relative to `backend/app/api/` unless it starts with `app/`). `v1/` also holds helper modules that are not routers: `helpers.py`, `binding_access.py`, `graph_pagination.py`, `integration_catalog.py` (`PROVIDER_CATALOG`), `integration_testing.py` (`test_provider_connectivity`), `agent_import.py`, `mcp_transport.py`, `platform_settings_helpers.py`, `versioning_helpers.py`.

| Prefix                   | File                   | Purpose                                      | Auth required        |
| ------------------------ | ---------------------- | -------------------------------------------- | -------------------- |
| `/api/v1/auth`           | `v1/auth.py`           | Register, login, get current user, auth config | Public (config, register, login) / Authenticated (me) |
| `/api/v1/invitations`    | `v1/invitations.py`    | Admin-generated, email-bound, single-use invitation links | **Admin** (`GET /validate/{token}`: Public) |
| `/api/v1/settings`       | `v1/settings.py`       | LLM providers, general assistant, user preferences, SSO, OAuth defaults, Langfuse, observability, security options, model pricing, quotas, disabled providers, memory purge, global MCP key | Every write **Admin** except `PUT /preferences` and `/me/memory`; SSO, Langfuse, observability, security options, model pricing, quotas, disabled providers: **Admin** reads too; provider and model lists, general assistant, OAuth defaults, tags, `my-quotas`: Authenticated |
| `/api/v1/settings/model-announcements` | `v1/model_announcements.py` | Email every user the new default model (preview, test, send) | **Admin** |
| `/api/v1/exposure`       | `v1/resource_exposure.py` | Who can use a resource a person lends to others, and a preview of what a change would lend or take back (read-only) | Authenticated |
| `/api/v1/integrations`   | `v1/integrations.py`   | CRUD, catalog (`GET /catalog`, from `integration_catalog.PROVIDER_CATALOG`), types, events, OAuth authorize, per-user credentials, test, tools, company-managed toggle | Authenticated (toggle: **Admin**; `GET /types`, `GET /events`: no auth) |
| `/api/v1/integrations/{id}/openrouter` | `v1/openrouter.py` | OpenRouter console | Authenticated, the integration's managers (`llm-provider` create / models / unlink: **Admin**) |
| `/api/v1/integrations` (OpenAPI) | `v1/integration_openapi.py` | OpenAPI / Swagger integrations: spec preview, detail, operation selection, refresh, replace, client certificate, device-code poll, implicit token hand-off (spec loads rate-limited per user, URL loads audited; every route closed by the provider kill switch) | Authenticated (edits: the integration's managers) |
| `/api/v1/integrations` (elements) | `v1/integration_elements.py` | List selectable elements (Teams/Slack channels, GitHub repos) | Authenticated |
| `/api/v1/integrations` (notifications) | `v1/integration_notifications.py` | MS Graph + Slack + LinkedIn webhook notification handlers | Public (Slack: signature, Graph: `clientState`, LinkedIn: `X-LI-Signature` with the admin app's client secret; a LinkedIn delivery is handed over by the provider webhook's rule, see "Webhook API") |
| `/api/v1/integrations/{id}/github-webhooks` | `v1/integration_github_webhooks.py` | Managed GitHub repository webhooks: list, create, delete | Authenticated |
| `/api/v1/integrations` (exposure) | `v1/integration_exposure.py` | Who can act with an integration's stored credential (`/{id}/exposure`, `/exposure/summary`, `/exposure/preview`, read-only) | Authenticated |
| `/api/v1/channel-bots`   | `v1/channel_bots.py`   | Channel bot CRUD (always company-managed), catalog, tools, test, channels (Slack, Teams, WhatsApp, Telegram, Discord, Google Chat) | Authenticated (create, update, delete: **Admin**) |
| `/api/v1/agent-accounts` | `v1/agent_accounts.py` | Agent accounts: CRUD, sync, decisions, rule simulator, pickers, approval queue (`/replies`); `/notifications/{provider}` is the providers' push endpoint | Authenticated, owner-only (admins: every account, `?scope=all`); notifications: public, `clientState` |
| `/api/v1/agents`         | `v1/agents.py`         | Agent CRUD, duplicate, import/export, company-managed toggle | Authenticated (toggle: **Admin**) |
| `/api/v1/agents` (templates) | `v1/agent_templates.py` | Agent template CRUD | List: Authenticated; create, update, delete: **Admin** |
| `/api/v1/agents` (chats) | `v1/agent_chats.py` | Agent conversations across all channels (Chats tab) | Authenticated |
| `/api/v1/agents` (memory) | `v1/agent_memory.py` | Agent memory list + semantic search | Authenticated |
| `/api/v1/agents/{agent_id}/a2a` | `v1/a2a_admin.py` | An agent's A2A exposure status (read-only) | Authenticated |
| `/api/v1/agents/{agent_id}/a2a/partner-fleet-reads` | `v1/a2a_admin.py` | PUT: the partner fleet reads opt-in of the live exposure (#2202) | Authenticated |
| `/api/v1/agents/{agent_id}/a2a/partner-owner-reads` | `v1/a2a_admin.py` | PUT: the partner owner reads opt-in of the live exposure (#2638), audited | Authenticated |
| `/api/v1/skills`         | `v1/agent_skills.py`   | Agent skill CRUD, versions, packages (validate, import, export), refresh from URL, company-managed toggle | Authenticated (toggle: **Admin**) |
| `/api/v1/prompts`        | `v1/prompts.py`        | Prompt CRUD, versions, export/import, company-managed toggle | Authenticated (toggle: **Admin**) |
| `/api/v1/assignments`      | `v1/assignments.py`      | Assignment CRUD, manual trigger, runs, HITL respond/dismiss, follow-up, purge | Authenticated (`DELETE /runs/purge`, `POST /runs/cleanup-stale`: **Admin**) |
| `/api/v1/knowledge-bases`| `v1/knowledge_bases.py`| KB CRUD, documents, sync sources, crawl, test search, activity, export/import, company-managed toggle | Authenticated (toggle, `POST /global/reprocess-stale`, embedding stats and dimension purge: **Admin**) |
| `/api/hooks`             | `v1/webhooks.py`       | Webhook receiver for assignment triggers (`/{agent_id}/{path}`, `/providers/{provider}`, `/integrations/{id}`) | Public (external) |
| `/api/v1/tasks`          | `v1/tasks.py`          | List task history                              | Authenticated        |
| `/api/v1/activity`       | `v1/activity.py`       | List activity, stats, adoption, traces, SSE stream | Authenticated (stream: the JWT in `?token=`) |
| `/api/v1/learning`       | `v1/learning.py`       | Document ingestion, style learning             | Authenticated        |
| `/api/v1/annotations`    | `v1/annotations.py`    | Ratings of agent work                          | Authenticated        |
| `/api/v1/feedback-criteria` | `v1/feedback_criteria.py` | Feedback criteria                        | List: Authenticated; create, update, delete: **Admin** |
| `/api/v1/improvement-proposals` | `v1/improvement_proposals.py` | Improvement proposals (persona rewrites and Platform Health's changes) | Authenticated (list / get: the target's managers; `POST /persona`: **Admin**; approve / reject: the target's managers) |
| `/api/v1/traces`         | `v1/traces.py`         | Native LLM trace storage: list, detail, stats, analytics, export bundles, scores | Authenticated (per-user / per-group cost, export bundles, scores, delete: **Admin**) |
| `/api/v1/hitl-whitelist` | `v1/hitl_whitelist.py` | The platform-wide list of tools exempt from human approval | **Admin** (`GET /catalog`: Authenticated) |
| `/api/v1/tool-approvals` | `v1/tool_approvals.py` | The caller's own standing "Yes, always" approvals | Authenticated (`GET /usage`: **Admin**) |
| `/api/v1/work-items`     | `v1/work_items.py`     | Durable work-item queue, scheduled work items  | Authenticated        |
| `/api/v1/shared-state`   | `v1/shared_state.py`   | Cross-run shared state store                   | Authenticated        |
| `/api/v1/worksets`       | `v1/worksets.py`       | Working corpora agents build: list, items, resolve, delete | Authenticated |
| `/api/v1/workspaces`     | `v1/workspaces.py`     | Workspace CRUD, default workspace              | Authenticated (agentic licence gate) |
| `/api/v1/mcp-servers`    | `v1/mcp_servers.py`    | MCP server CRUD, OAuth discover / authorize, test, refresh tools, export/import, company-managed toggle | Authenticated (update, delete: the server's managers; toggle and company-managed create: **Admin**) |
| `/api/v1/custom-tools`   | `v1/custom_tools.py`   | Custom tool CRUD, test execution, company-managed toggle | Authenticated (toggle: **Admin**) |
| `/api/v1/datasets`       | `v1/datasets.py`       | Evaluation dataset CRUD                        | **Admin**            |
| `/api/v1/evaluations`    | `v1/evaluations.py`    | Agent evaluation runs and scoring              | **Admin**, except `GET /families` (authenticated); writes also need AI enabled (create: agentic AI), cancel and run deletion do not; `POST /resume`: an external episode callback, `X-Signature` (`dev-docs/reference/evaluations.md`) |
| `/api/v1/variables`      | `v1/variables.py`      | Variable CRUD                                  | Authenticated (404 under `DISABLE_CUSTOM_VARIABLES`) |
| `/api/v1/template-containers` | `v1/template_containers.py` | Template container CRUD, metadata filtering, assets | Authenticated |
| `/api/v1/presentations`  | `v1/presentations.py`  | Agent-generated slide decks: read, edit, versions, comments, presence, public link | Authenticated (`render-spec`, `render-asset`: the render token) |
| `/api/v1/browser-profiles` | `v1/browser_profiles.py` | Browser profiles, saved sign-ins every browser of the platform opens (agents' `browser` tool, General Assistant, Video Studio): list, create, read, update, delete, `sign-in`, `test-sign-in` (a dry run returning the trace), `check` (the saved session probed), `sign-out`, `mfa-screenshot`, MFA codes on `GET` / `POST /mfa/{challenge_id}`. Not behind the Video Studio flag | Authenticated; list, sign-in, test-sign-in, check and MFA: the visibility predicate WITHOUT the admin bypass (using a login is an execution power); read: the predicate; update, delete, sign-out: `can_manage` |
| `/api/v1/video-studio` | `v1/video_studio.py` | Video Studio: projects (spec, operations, preview, `quality`, renders, duplicate, sharing and company-managed toggle on `PATCH`, `public-link` `GET` / `POST` / `PATCH` / `DELETE`), renders (keep, make current, cancel, delete with their files; `POST /projects/{id}/renders/cleanup` many at once by selector or id, `dry_run` to preview), `render-retention` (`GET` / `PUT`, the days renders are kept), video templates, recordings, AI clips and music tracks (`music`, `music/library`), files, `active` jobs, confined media URLs (`media-token`, `/media/{token}`, `/music-bed/{token}`) and the compositor `preview-page`; libraries paginated (`limit` / `offset`, `{items, total}`, search in SQL): `GET /assets` (recordings and AI shots together), `GET /takes`; `GET /clips/start-frame` (the frame an AI shot would open on, before anything is paid for), `POST /pictures/compose` (real pictures drawn inside a picture), `GET /voice/{token}` (the narration the editor's preview plays: the token, no session) | Behind the `VIDEO_STUDIO` feature flag (`require_feature`: off, every route is a 403 and none is documented). Authenticated, except `/media/{token}`, `/music-bed/{token}` (the scoped token) and `preview-page` (no data); rendering, AI clips and music are behind the AI gate (`require_ai_enabled`); starting a render, keeping or making one current, sharing and the public link: the video's managers (deleting a render, alone or by a cleanup: also who asked for it; cancelling: who asked for it or an admin); `PUT /render-retention`: administrators |
| `/api/v1/reports`        | `v1/reports.py`        | Agent-generated reports (read, export)         | Authenticated (`render-spec`: the render token) |
| `/api/v1/investigations` | `v1/investigations.py` | Deep Investigation runs (read-only)            | Authenticated        |
| `/api/v1/investigation-packs` | `v1/investigation_packs.py` | Deep Investigation packs: CRUD, versions, builder | Authenticated |
| `/api/v1/files`          | `v1/files.py`          | Cross-context file repository (list, delete)   | Authenticated        |
| `/api/v1/flows`          | `v1/flows.py`, `v1/flow_runs.py` | Flow builder CRUD, catalog, validation, versions; activation, runs, debug, cancel | Authenticated |
| `/api/v1/flow-event-types` | `v1/flow_event_types.py` | Typed flow event declarations            | Authenticated        |
| `/api/v1/nova-rules`     | `v1/nova_rules.py`     | Nova prompt-safety rules, sources, engine status | Authenticated (company-managed rules: **Admin**) |
| `/api/v1/intents`, `/api/v1/verticals` | `v1/intents.py` | Intent + vertical CRUD and agent bindings | Read: Authenticated; create, update, delete: **Admin** |
| `/api/v1/users`          | `v1/users.py`          | User management (list, role, active toggle, quotas, impersonate) | **Admin** (`GET /directory`: Authenticated) |
| `/api/v1/groups`         | `v1/groups.py`         | Group CRUD, membership management              | **Admin** (list: Authenticated) |
| `/api/v1/audit-logs`     | `v1/audit_logs.py`     | Searchable audit trail (list, filter by action/entity/user) | **Admin** |
| `/api/v1/admin/objects`  | `v1/admin_objects.py`  | Admin object browser                           | **Admin**            |
| `/api/v1/hooks`          | `v1/hooks.py`          | Platform event hook registration               | **Admin** (`GET /events`: Authenticated) |
| `/api/v1/xtm-hub`        | `v1/xtm_hub_registration.py` | This platform's registration with the XTM Hub: read, register (the platform token the Hub's page posted back, stored once the Hub confirms it, never served), register automatically (`PUT /registration/auto`: the token the Hub issued for a trial deployment, sent by its deployer with the `ADMIN_TOKEN` key; 204, 400 not a token, 502 refused or unreachable), check now, forget locally; `GET /registration/status` is the coarse status the dashboard pill reads, served to every member with the Hub URL and `default_hub_url` (`XTM_HUB_DEFAULT_URL`, the product switcher's XTM Hub link) (the pill links to it, so a self-hosted Hub's address is readable by every member). Rules: `services/xtm_hub_registration.py` | **Admin** (status: Authenticated) |
| `/api/v1/security-audit` | `v1/security_audit.py` | Security configuration audit (Settings > Security): findings, and every security control of `app/core/security_controls.py` with its state; every `AuditFinding` names a literal `id="<category>.<name>"`, pinned by `tests/test_security_audit_finding_ids.py` (see `dev-docs/reference/backend-api-security-controls.md`); `PUT` / `DELETE /findings/{id}/dismissal` dismiss and restore a reported non-pass finding (`services/security_audit_finding_states.py`, audited), which the report marks `dismissed` and leaves out of `summary` / `score`; the GET writes nothing (a re-opening is recorded by the hourly notifier and the two write routes) | **Admin** |
| `/api/v1/csp-report`     | `v1/csp_reports.py`    | Browser Content-Security-Policy violation reports (bounded before read) | Public |
| `/api/v1/chat/*`         | `v1/chat.py`           | Conversations, messages (REST send), files, TTS. `PUT /conversations/{id}/agent` continues an existing web conversation with another agent in place: owner only, an agent the caller could start a conversation with (`_chat_agent`, `null` = the General Assistant), 409 while a turn runs; the history stays, a note "Continued by <agent>" is written (`conversation_continuity.continue_with_agent`). Conversations carry `deleted_agent_id` / `deleted_agent_name`, messages `agent_switch` | Authenticated        |
| `/api/v1/web/*`          | `v1/chat_web_stream.py` | The web chat over HTTP + SSE (`@filigran/chatbot`): send and events streams, steer / approve / cancel / skip-wait, sessions, upload | Web session JWT; send, steer, approve, `POST /chat/sessions` and upload also `require_chat_enabled` |
| `/api/v1/chat/commands*` | `v1/chat_commands.py` | Chat slash commands: list, run `/context` and `/help` | Authenticated, chat enabled |
| `/api/v1/chat` (continuation) | `v1/chat_continuation.py` | Continue a conversation in Slack or Teams (`/conversations/{id}/channel-bindings`) | Authenticated (the conversation owner) |
| `/api/v1/arcade`         | `v1/arcade.py`         | Waiting arcade                                 | Authenticated (404 when `ARCADE_ENABLED=false`) |
| `/api/v1/ai`             | `v1/ai.py`             | AI utilities (prompt and template generation, agent wizard assistant) | Authenticated |
| `/api/v1/runners`        | `v1/runners.py`        | Native runners / XTM One Desktop: enrollment keys, machines, permissions, held commands, runner transport, self-update | Authenticated (runner transport: the runner credential; capabilities, update manifest and downloads: Public) |
| `/api/v1/panic`          | `v1/panic.py`          | Emergency stop: status, stop, resume, emergency token | Authenticated (emergency token management: **Admin**; `POST /emergency`: the emergency bearer token) |
| `/api/v1/relays`         | `v1/relays.py`         | XTM One Relay: relay groups, routed resources, enrollment keys, relays, relay-facing transport | Authenticated (relay transport: the relay credential or enrollment key) |
| `/api/v1/desktop-login`  | `v1/desktop_login.py`  | Browser sign-in handoff for XTM One Desktop    | `start` / `exchange` / `DELETE`: the runner credential; `GET /{id}`: Public; `complete` / `decline`: Authenticated without the Terms of Use gate |
| `/api/v1/extension`      | `v1/extension.py`      | Browser extension execute-task proxy           | Authenticated        |
| `/api/v1/api-keys`       | `v1/api_keys.py`       | The caller's personal API keys (`fcp-...`), policy | Authenticated   |
| `/api/v1/admin/api-keys` | `v1/api_keys.py` (`admin_router`) | Every personal API key of the platform, revoke | **Admin** |
| `/api/v1/ai-adoption`    | `v1/ai_adoption.py`    | AI Adoption dashboard (agentic AI only): overview, gaps | **Admin** |
| `/api/v1/ctem-metrics`   | `v1/ctem_metrics.py`   | CTEM metrics                                   | Authenticated (stage agents: **Admin**) |
| `/api/v1/platform`       | `v1/platform.py`, `v1/platform_chat_workspaces.py`, `v1/platform_access_notices.py` | External platform registration (OpenCTI / OpenAEV / OpenCRQ), platform config and branding, XTM MCP snippets, embedded chat proxy and its workspaces, the decks, reports and videos of its answers (`GET /chat/artifacts/{kind}/{id}`, `POST .../download`, `services/platform_chat_artifacts.py`), the access notice a platform has XTM One email (`POST /access-notices`, `services/platform_access_notices.py`) | `/config`, branding, manifest: Public; `register`: the registration token (`Authorization`); registration token, approval, purge: **Admin**; `access-notices`: a token signed by an active registration; the rest Authenticated |
| `/api/v1/a2a/client`     | `v1/a2a_client.py`     | Read, verify and repair an outbound A2A registration | Authenticated |
| `/a2a/{card_token}`      | `v1/a2a_server.py`     | A2A inbound server: agent card, JSON-RPC tasks | Card: Public; JSON-RPC: the partner's credential |
| `/a2a/callbacks/{delegation_id}` | `v1/a2a_callbacks.py` | A partner's notification that a delegated task changed state | Public (partner-authenticated, rate limited before auth) |
| `/api/v1/public/*`       | `v1/public_chat.py`    | Public chat widget (web-embedded agents), widget.js, logo | Public (token-based) |
| `/api/v1/public/catalog/*` | `v1/public_catalog.py` | Filigran AI Catalog — browse & clone published entities (`is_public_catalog=True`) | Authenticated |
| `/api/v1/public/presentations/{token}` | `v1/presentation_public.py` | A presentation's public link: deck, assets, PDF | Public (the token) |
| `/api/v1/public/videos/{token}` | `v1/video_public.py` | A video's public link: its title and current render (`PublicVideoResponse`), `/media/{video,poster,captions}` by byte ranges, `?download=true` when the link allows it. An unknown, regenerated or disabled token, a deleted video, the admin switch or `VIDEO_STUDIO` off: the same 404; `no-store` + `no-referrer`, rate limited per client address | Public (the token) |
| `/v1/*`                  | `v1/llm_proxy.py`      | LLM proxy: OpenAI-compatible models list + chat completions, Anthropic-compatible messages + token count | API key auth |
| `/mcp/user`              | `v1/mcp_endpoint.py`   | MCP agent endpoint — user mode (Streamable HTTP, exposes user-selected agents) | API key auth (`fcp-...`) |
| `/mcp/global`            | `v1/mcp_endpoint.py`   | MCP agent endpoint — global mode (Streamable HTTP, exposes admin-selected agents) | MCP key auth (`mcp-...`) |
| `/mcp/opencti`, `/mcp/openaev`, `/mcp/opencrq` | `v1/mcp_platform.py` | Platform MCP servers: the registered platform's business tools | Bearer: `fcp-...` key or OAuth 2.1 access token |
| `/mcp/xtm`               | `v1/mcp_xtm.py`        | XTM One's own management and observability tools over MCP | Bearer: `fcp-...` key or OAuth 2.1 access token |
| `/mcp/oauth/*`           | `v1/mcp_oauth.py`      | OAuth 2.1 authorization server (authorize, token, revoke) | Public (OAuth flows) |
| `/.well-known/*`         | `v1/mcp_oauth.py` (`well_known_router`) | OAuth / OpenID metadata (RFC 8414 / RFC 9728) | Public |
| `/xtm/auth/*`            | `v1/xtm_auth.py`       | JWKS and metadata for cross-platform JWT verification | Public |
| `/metrics`               | `v1/metrics.py`        | Prometheus metrics                             | `PROMETHEUS_BEARER_TOKEN` when set (off unless `PROMETHEUS_ENABLED`) |
| `/api/channels/slack/*`  | `app/channels/slack.py` | Slack Events API webhook                      | Public (Slack-signed) |
| `/api/channels/teams/*`  | `app/channels/teams.py` | Teams Bot Framework webhook                   | Public (Bot Framework) |
| `/api/channels/whatsapp/*` | `app/channels/whatsapp.py` | WhatsApp Cloud API webhook                 | Public (Meta-verified) |
| `/api/channels/telegram/*` | `app/channels/telegram.py` | Telegram Bot API webhook                   | Public (Telegram-verified) |
| `/api/channels/discord/*` | `app/channels/discord.py` | Discord bot interactions                    | Public (Discord-signed) |
| `/api/channels/googlechat/*` | `app/channels/google_chat.py` | Google Chat webhook                   | Public (Google-signed) |
| `/api/auth/oidc/*`       | `auth_sso.py`          | OIDC login, callback                           | Public               |
| `/api/auth/saml/*`       | `auth_sso.py`          | SAML login, ACS, SP metadata (with group mapping) | Public            |
| `/api/oauth/callback`    | `oauth_callbacks.py`   | OAuth redirect callback (unified)              | Public (browser redirect) |
| `/api/health`            | `app/main.py`          | Health check endpoint                          | Public               |

## Agent API Detail

- `GET /agents/templates` — list available agent templates
- `POST /agents/templates` — create new template (**Admin**)
- `PUT /agents/templates/{slug}` — update template (**Admin**)
- `DELETE /agents/templates/{slug}` — delete template (**Admin**)
- `GET /agents` — list user's agents (includes company-managed)
- `POST /agents` — create agent (from template or scratch, auto-creates assignments)
- `GET /agents/channel-routing` — get channel routing info for all agents
- `GET /agents/budget-policy` — the platform run-budget ceilings + whether the caller may exceed them; declared before `/{agent_id}`. Not admin-gated: a user sees the ceilings they are bound by.
- `GET /agents/{id}` — get agent details
- `PUT /agents/{id}` — update agent (persona, bindings, config, `budget_config`, `is_company_managed`, etc.). `browser_profile_ids` (create and update): a profile the write ADDS must be one the writer may use, without the admin bypass, and company-managed for a company-managed agent (400 otherwise); `browser_profile_read_only` keeps only bound ids
- `DELETE /agents/{id}` — delete agent + all assignments. Its conversations and traces are kept: `agent_deletion` writes its tombstone (`agent_tombstones`) and marks its conversations `deleted_agent_id`. `?successor_agent_id=` names the agent they continue with, for the owners who can use it (an agent the caller can use, enabled; 400 otherwise, checked before anything is deleted), each with a note "Continued by <agent>" (`conversation_continuity.record_agent_deletion`)
- `DELETE /agents/{id}/channel-config/{channel_type}` — delete agent channel config for a channel type
- `POST /agents/{id}/duplicate` — duplicate an agent with all its assignments
- `GET /agents/{id}/export` — export agent config as JSON
- `POST /agents/import` — import agent from exported JSON
- `POST /agents/{id}/generate-suggested-prompts` — generate suggested prompts for agent web chat

`budget_config` is gated: raising a limit above the platform's
`AGENT_LOOP_MAX_*` value is admin-only, which is what makes the env setting a
budget a user cannot exceed alone. Two shapes, by whether the caller authored
the value — `helpers.assert_budget_within_ceiling` raises 403 on create/update,
`helpers.lower_budget_to_ceiling` silently lowers on duplicate/import (as
`clamp_max_iterations` does there). The `flows` endpoints
carry the graph-level twins for `capability.budget` nodes, which are the same
policy written on a canvas. See `dev-docs/reference/backend-tools-run-budget.md` § "The override layer".

Duplicate, export and import all take the set of fields they carry from the
manifests in `app/models/agent_copy.py` — never from a kwargs list written at
the call site.

## Assignment API Detail

- `GET /assignments?agent_id={id}` — list assignments (optional agent filter)
- `POST /assignments` — create assignment
- `PUT /assignments/{id}` — update assignment
- `DELETE /assignments/{id}` — delete assignment
- `POST /assignments/{id}/trigger` — manually trigger a assignment
- `GET /assignments/{id}/runs?limit=20` — execution log for a assignment
- `DELETE /assignments/{id}/runs` — delete all runs for a assignment
- `GET /assignments/runs/all` — list all runs across assignments
- `GET /assignments/runs/count` — get total run count (with optional filters)
- `GET /assignments/runs/{run_id}` — get a specific run
- `DELETE /assignments/runs/{run_id}` — delete a specific run
- `DELETE /assignments/runs/purge` — purge old assignment runs (**Admin**)
- `POST /assignments/runs/cleanup-stale` — cleanup stale/stuck runs (**Admin**)
- `POST /assignments/runs/bulk-delete` — bulk delete multiple runs
- `POST /assignments/runs/bulk-cancel` — cancel every run not started yet (`pending` / `delayed` / `queued`) of an agent, an assignment and / or created before a moment; `dry_run` answers the count; reason `Cancelled by <user>`, audited `bulk_cancel_runs` (**Admin**; `services/run_bulk_cancel.py`, shared with the agent tool `cancel_runs`). UI: `useBulkCancelRuns` (`hooks/api/useAssignmentApi.ts`), the admin action "Cancel runs not started" of Operations > Executions (`pages/TaskHistory.tsx`): scoped to the agent filter, dry run then confirmation with the count, then the `cancelled` status filter
- `POST /assignments/runs/{run_id}/respond` — HITL: respond to a human-input request
- `POST /assignments/runs/{run_id}/dismiss` — HITL: dismiss a human-input request
- `POST /assignments/runs/{run_id}/followup` — followup on a completed run (never more visible than that run)

## Integration API Detail

- `GET /integrations/catalog` — provider types and required fields
- `GET /integrations/types` — integration type categories
- `GET /integrations/events` — available integration events
- `GET /integrations` — list integrations
- `POST /integrations` — create integration
- `GET /integrations/{id}` — get integration details
- `PUT /integrations/{id}` — update integration
- `DELETE /integrations/{id}` — delete integration
- `GET /integrations/{id}/authorize` — start OAuth flow. Only the owner of a personal integration, or an admin on a company-managed one, may start it — the only users whose token the callback stores (`helpers.can_authorize_integration()`, served on responses as `can_authorize`); anyone else gets a 403 before the provider's consent screen. It requests the provider catalog's current scopes without storing them: the callback records the scopes once they are granted, so an abandoned or denied consent leaves the scope drift visible
- `POST /integrations/{id}/test` — test integration connection
- `GET /integrations/{id}/elements` — list integration elements (e.g. channels, repos)
- `GET /integrations/{id}/notion-webhook` — Notion integration only, managers only: the webhook URL to subscribe in Notion, the verification token Notion sent at the subscription handshake (`services/notion_webhooks.py`), the events
- `GET /integrations/{id}/tools` — list tools provided by this integration
- `POST /integrations/notifications/microsoft` — Microsoft Graph notification webhook. Each notification must echo the `clientState` of its subscription, stored Fernet-encrypted in the record only `store_graph_subscription()` writes (`services/integration_notification_auth.py`); any other is discarded before its assignment, integration or token is read, with the same answer
- `POST /integrations/notifications/slack` — Slack event notification webhook. `X-Slack-Signature` checked before the body is parsed; every Slack integration is bound to the signing secret of its app (the Slack OAuth default's when it runs on the admin app, else its own `webhook_secret`, the "Signing secret" field) and a delivery is authentic only for the integrations whose app secret signed it. A delivery no held secret proves never reaches an integration whose app secret is configured; by default it is handed out to every assignment not connected only to integrations whose app secret is configured (rate-limited WARNING, Security Audit finding) and the url_verification handshake is still answered, while the Settings > Security option "Require verified integration notifications" (`INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION`, read through `verification_required()`) makes anything unverified a 401 before the body is read (those integrations then rely on the poller). A verified message reaches only the integrations of the workspace it was sent for and the assignments connected to them under the poller's rules (the token gate for a fixed binding; a run whose acting user cannot see the integration is delivered and logged), and each assignment runs once per message whatever the poller or a Slack retry does (`services/slack_events.py`)

## Knowledge Base API Detail

- `GET /knowledge-bases` — list knowledge bases
- `POST /knowledge-bases` — create knowledge base
- `GET /knowledge-bases/{id}` — get KB details
- `PUT /knowledge-bases/{id}` — update KB
- `DELETE /knowledge-bases/{id}` — delete KB
- `POST /knowledge-bases/{id}/documents/text` — add text/markdown document
- `POST /knowledge-bases/{id}/documents/upload` — upload file document
- `POST /knowledge-bases/{id}/documents/crawl` — crawl a URL and add content
- `DELETE /knowledge-bases/{id}/documents/{doc_id}` — delete a document
- `POST /knowledge-bases/{id}/documents/reprocess` — reprocess all documents
- `POST /knowledge-bases/{id}/test-search` — test semantic search
- `GET /knowledge-bases/{id}/activity` — KB activity log
- `GET /knowledge-bases/{id}/sync-sources` — list sync sources
- `POST /knowledge-bases/{id}/sync-sources` — add sync source
- `PUT /knowledge-bases/{id}/sync-sources/{source_id}` — update sync source
- `DELETE /knowledge-bases/{id}/sync-sources/{source_id}` — delete sync source
- `POST /knowledge-bases/{id}/sync-sources/{source_id}/trigger` — manually trigger sync
- `POST /knowledge-bases/{id}/sync/notion` — sync from Notion
- `POST /knowledge-bases/{id}/sync/microsoft-drive` — sync from OneDrive/SharePoint
- `POST /knowledge-bases/{id}/sync/google-drive` — sync from Google Drive
- `POST /knowledge-bases/{id}/sync/github` — sync from GitHub repo

## Webhook API

- `POST /api/hooks/{agent_id}/{path}` — receives external HTTP POST
- `POST /api/hooks/providers/{provider}` — the admin OAuth app's webhook; with the "Require verified integration notifications" option on (`INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION`) reaches only assignments bound to an integration on that app (by default every assignment matching the provider and event, the ones the rule would leave out logged and audited) - `integration_state.uses_admin_oauth_app`: the recorded `uses_admin_oauth_credentials`, else the stored client id against the OAuth default's - that the agent owner may use, and for GitHub only integrations recording the delivery's installation or, recording none, whose own token sees it (`_admin_app_integrations` in `webhooks.py`; the token half is `services/github_token_installations.py`: the delivery reads cached answers only and never asks GitHub; an integration whose answer is not cached yet is reached while it is looked up in the background); `_admin_app_integrations_by_assignment` is the one place that hands a delivery over by that rule, shared with `POST /api/v1/integrations/notifications/linkedin` (a LinkedIn delivery, `X-LI-Signature`-checked, goes through the same option and default) - never re-spell it in another route
- `POST /api/hooks/integrations/{integration_id}` — integration-specific webhook. A `docusign` integration takes DocuSign Connect deliveries here (`webhooks._handle_docusign_connect`): refused with 401 unless one `X-DocuSign-Signature-<n>` is the base64 HMAC-SHA256 of the raw body under one of the integration's `connect_hmac_key` values (no key: every delivery refused), then `services/docusign_connect.dispatch_connect_delivery`; the generic dedup cache and fan-out are not used for it.
- `POST /api/hooks/integrations/{integration_id}/assignments/{assignment_id}` — integration+assignment webhook
- Matches assignments by `agent_id` + `trigger_config.path`
- Supports GitHub event filtering via `X-GitHub-Event` header + `action` field
- Returns `{ status, triggered: [{assignment, run_id}], count }`

## Settings API Detail

- `GET /settings/llm-providers` — list configured LLM providers (platform-level, `user_id IS NULL`)
- `POST /settings/llm-providers` — create LLM provider (**Admin**). Auto-sets platform default if none exists.
- `PUT /settings/llm-providers/{id}` — update provider (available_models, etc.) (**Admin**)
- `GET /settings/llm-providers/{id}/deletion-impact` — what deleting the provider does to every route that uses it (**Admin**, same guards as the delete; optional `replacement_provider_id`; nothing written; its `impact_fingerprint` is the delete's `expected_impact`)
- `DELETE /settings/llm-providers/{id}` — remove provider (**Admin**). Optional `replacement_provider_id` (422 unless a surviving chat-capable config); locks every provider config row first, so a concurrent deletion or catalog edit is serialized and the replacement re-validated; optional `expected_impact` (the preview's `impact_fingerprint`) makes it conditional - 409 `deletion_impact_changed` carrying the fresh preview when what it would apply changed since; applies exactly what the deletion impact describes - chat defaults re-seeded and pinned to the replacement, image default moved when a survivor can serve images, embedding default cleared, pinned agents / user preferences reset - commits before responding, and records the replacement per route on the audit entry
- `GET /settings/llm-providers/{id}/models` — list models for a specific provider
- `GET /settings/available-models` — list all available chat models across providers
- `GET /settings/available-embedding-models` — list all available embedding models
- `GET /settings/available-image-models` — list all available image models
- `GET /settings/available-decision-models` — list the decision models (CLMs) the providers report, for the platform decision model picker; `listings` gives each listing provider's status (listed / empty / incomplete / failed / unavailable) so Settings says why one offered none
- `GET /settings/available-video-models` — list all available video models: the Video Studio's listing (`router.list_video_models_for`), so a model its provider no longer serves is left out
- `GET /settings/video-model-status` — why the video model chosen in Settings can no longer make a video (a retired model), if it cannot
- `GET /settings/available-speech-models`, `GET /settings/speech-voices` — speech models and their voices
- `GET /settings/available-music-models` — the music generation models across providers (Eleven Music on ElevenLabs, Lyria on Gemini)
- `GET /settings/general-assistant` — get general assistant config (model + system prompt)
- `PUT /settings/general-assistant` — update general assistant config (**Admin**). `skill_ids`: enabled company-managed skills only (422 otherwise); null = the built-in General Assistant skills (`services/general_assistant_skills.py`)
- `POST /settings/llm-providers/resolve-models` — resolve the models a provider name + API key serves, before saving (**Admin**)
- `PUT /settings/preferences` — update user preferences (LLM model, default KBs, MCP servers, favorite agents, closed OAuth-share reminders, the CTEM and Copilot dashboard layouts - widget ids checked by `services/dashboard_layouts.py` -, `ga_browser_profile_ids` - a profile the save ADDS must be one the person may use, 403 otherwise), read back on `GET /auth/me`. `favorite_agent_ids` is normalized server-side: de-duplicated, ids that name no agent dropped (this is what makes `purge_agent_references` stick against a client holding a stale user), 422 on a non-UUID entry or more than `MAX_FAVORITE_AGENTS` (100). `oauth_share_dismissed_integration_ids` (the dashboard's "N of your integrations share your personal OAuth credentials" reminder, closed with the X) is normalized the same way (`MAX_OAUTH_SHARE_DISMISSALS`, 100) and additionally scoped: only ids naming an OAuth integration the caller OWNS whose token is currently acknowledged as shared (`oauth_share_acknowledged`) are kept — the exact condition under which the reminder shows, so nothing else can be muted. `PUT /integrations/{id}` drops an id from its OWNER's list the moment `integration_credentials.apply_oauth_share_consent` resets the acknowledgement (the integration stops being shared, or its owner opts out of sharing their connected account), and `DELETE /integrations/{id}` drops it with the row (`_forget_oauth_share_dismissal`, both), so re-sharing brings the reminder back and a deleted id never counts against the cap; the frontend's `useUpdateIntegration` / `useDeleteIntegration` invalidate `["me"]` for the same reason. The shared parsing lives in `_parse_uuid_list`
- `GET /settings/sso` — get SSO configuration (**Admin**)
- `PUT /settings/sso` — update SSO configuration (**Admin**)
- `GET /settings/oauth-defaults` — list OAuth defaults (which providers have one)
- `POST /settings/oauth-defaults` — create/update OAuth default (**Admin**)
- `DELETE /settings/oauth-defaults/{provider}` — delete OAuth default (**Admin**)
- `GET /settings/langfuse` — get Langfuse config (**Admin**)
- `PUT /settings/langfuse` — update Langfuse config (**Admin**)
- `POST /settings/langfuse/test` — test Langfuse connection (**Admin**)
- `GET /settings/tags` — list all entity tags
- `GET /settings/disabled-providers` — list globally disabled integration providers (**Admin**)
- `PUT /settings/disabled-providers` — replace the disabled integration providers; `PUT` / `DELETE /settings/disabled-providers/{provider}` for one (**Admin**)
- `POST /settings/purge-all-memories` — emergency purge of all agent memories across the platform (**Admin**)
- `POST /settings/mcp-endpoint/generate-key` — generate global MCP API key for `/mcp/global` endpoint (**Admin**)
- `DELETE /settings/mcp-endpoint/revoke-key` — revoke global MCP API key (**Admin**)

## MCP Agent Endpoint Detail

- `POST /mcp/user` — MCP Streamable HTTP endpoint (user mode). Authenticated with user API key (`fcp-...`). Exposes user-selected agents (from `User.mcp_exposed_agent_ids`) as MCP tools. Supports JSON-RPC 2.0 methods: `initialize`, `tools/list`, `tools/call`, `ping`; a notification is never dispatched (`202`). Every MCP endpoint reads its body through `v1/mcp_transport.py`: version negotiation, `MCP-Protocol-Version`, batches under 2025-03-26, `Origin` validation, GET / DELETE = 405.
- `POST /mcp/global` — MCP Streamable HTTP endpoint (global mode). Authenticated with platform-wide MCP key (`mcp-...`). Exposes admin-selected agents (from `PlatformSettings.mcp_exposed_agent_ids`) as MCP tools. Same JSON-RPC methods.

## Activity API Detail

- `GET /activity` — list activity feed items
- `GET /activity/stats` — activity statistics
- `GET /activity/adoption` — adoption statistics
- `GET /activity/traces` — Langfuse trace listing
- `GET /activity/stream` — SSE stream for real-time activity updates (the JWT as `?token=`)

## AI API Detail

- `POST /ai/generate-prompt` — generate a prompt using AI
- `POST /ai/generate-template` — generate a template using AI
- `POST /ai/agent-wizard/assist` — one round with the agent wizard's assistant

## LLM Proxy Detail

- `GET /v1/models` — list available models (OpenAI-compatible)
- `POST /v1/chat/completions` — chat completions (OpenAI-compatible)
- `POST /v1/messages`, `POST /v1/messages/count_tokens` — messages and token count (Anthropic-compatible)

## Public Chat API Detail

- `GET /public/widget.js` — embeddable chat widget JavaScript
- `GET /public/logo.svg` — Filigran logo for widget
- `GET /public/chat/{web_token}/config` — get agent config for public chat
- `POST /public/chat/{web_token}/sessions` — create public chat session
- `GET /public/chat/{web_token}/visitor/{visitor_id}/latest` — get latest session for visitor
- `GET /public/chat/{web_token}/sessions/{session_id}` — get session messages (an answer's files under `files`)
- `POST /public/chat/{web_token}/sessions/{session_id}/messages` and `.../messages/stream` — one turn per session: the send takes the session's turn (`chat_turn_guard.acquire_turn`, the `chat:processing` marker `is_processing` reads); a message sent while a turn runs is stored and answered with `{"queued": true}` (stream: a `queued` event) and the widget polls the session. The holder, once its answer is stored, answers every message stored meanwhile as ONE next turn on the latest (`_finish_widget_turn`, in the background), then releases; it looks again after the release, so a queued message always gets a turn. Without Redis every message is its own turn.
- `GET /public/chat/{web_token}/sessions/{session_id}/files/{file_id}` — download a file an answer of this session showed (bound to this web_embed conversation, owned by nobody; anything else is the same 404)
- `GET /public/chat/{web_token}/sessions/{session_id}/images/{image_id}` — an image generated for this session (a raster `ChatImage` bound to this web_embed conversation, owned by nobody; anything else is the same 404). The widget loads the platform addresses an answer shows (`/api/v1/chat/files/<id>`, `/api/v1/chat/images/<id>`) through these two session routes, in both rendering modes

## Channel Bot API Detail

- `GET /channel-bots/catalog` — list available channel bot providers and required fields
- `GET /channel-bots` — list channel bots (includes company-managed)
- `POST /channel-bots` — create channel bot (**Admin**; always company-managed)
- `PUT /channel-bots/{id}` — update channel bot (**Admin**)
- `DELETE /channel-bots/{id}` — delete channel bot (**Admin**)
- `GET /channel-bots/{id}/tools` — list tools provided by this channel bot
- `POST /channel-bots/{id}/test-tool` — test a channel bot tool
- `GET /channel-bots/{id}/channels` — list the messaging platform's channels (Slack, Teams)

## Group API Detail

- `GET /groups` — list groups with member counts (any authenticated user)
- `POST /groups` — create group (**Admin**)
- `GET /groups/{id}` — get group details (**Admin**)
- `PUT /groups/{id}` — update group (**Admin**)
- `DELETE /groups/{id}` — delete group (**Admin**)
- `GET /groups/{id}/members` — list group members with pagination (**Admin**)
- `POST /groups/{id}/members` — add members to group (**Admin**)
- `DELETE /groups/{id}/members/{user_id}` — remove member from group (**Admin**)

## Agent Skill API Detail

- `GET /skills` — list agent skills (own + group-shared + company-managed)
- `GET /skills/package-bindings` — per-skill Custom Tools / KBs a packaged skill carries onto its agents (declared before `GET /skills/{id}` so the literal path matches; skills with no packaged resources are omitted)
- `POST /skills` — create skill by URL (`{"url": "..."}`) or by content (`{"content": "...", "name": "..."}`)
- `POST /skills/upload` — create skill by file upload (multipart)
- `GET /skills/{id}` — get skill details
- `PUT /skills/{id}` — update skill (who `can_manage()` it: the owner, an admin, a `config` / `config_with_chats` grantee; built-in skills 403)
- `DELETE /skills/{id}` — delete skill (the creator or an admin only: a `config` grantee may edit but not delete; built-in skills 403)
- `POST /skills/{id}/refresh` — refresh skill content from URL

## Prompt API Detail

- `GET /prompts` — list prompts (own + group-shared + company-managed)
- `POST /prompts` — create prompt
- `GET /prompts/{id}` — get prompt details
- `PUT /prompts/{id}` — update prompt (who `can_manage()` it: the owner, an admin, a `config` / `config_with_chats` grantee)
- `DELETE /prompts/{id}` — delete prompt (same rule)

## Public Catalog API Detail

- `GET /public/catalog` — catalog summary (counts per entity type)
- `GET /public/catalog/tags` — tags of the published entities
- `GET /public/catalog/agents` — list published agents
- `GET /public/catalog/agents/{id}` — get published agent details (with assignments)
- `GET /public/catalog/agents/{id}/download` — download agent export bundle
- `POST /public/catalog/agents/{id}/add` — clone agent to user's account
- `GET /public/catalog/prompts` — list published prompts
- `GET /public/catalog/prompts/{id}` — get published prompt details
- `GET /public/catalog/prompts/{id}/download` — download prompt export bundle
- `POST /public/catalog/prompts/{id}/add` — clone prompt to user's account
- `GET /public/catalog/skills` — list published skills
- `GET /public/catalog/skills/{id}` — get published skill details
- `GET /public/catalog/skills/{id}/download` — download skill export bundle
- `POST /public/catalog/skills/{id}/add` — clone skill to user's account
- `GET /public/catalog/mcp-servers` — list published MCP servers
- `GET /public/catalog/mcp-servers/{id}` — get published MCP server details (with tools)
- `GET /public/catalog/mcp-servers/{id}/download` — download MCP server export bundle
- `POST /public/catalog/mcp-servers/{id}/add` — clone MCP server to user's account
- `GET /public/catalog/custom-tools` — list published custom tools
- `GET /public/catalog/custom-tools/{id}` — get published custom tool details
- `GET /public/catalog/custom-tools/{id}/download` — download custom tool export bundle
- `POST /public/catalog/custom-tools/{id}/add` — clone custom tool to user's account

Only entities with `is_public_catalog = True` are listed. Every endpoint requires JWT auth; downloads are `GET /public/catalog/{entity_type}/{id}/download` and add-to-account `POST /public/catalog/{entity_type}/{id}/add`, which clones the entity into the caller's account.
