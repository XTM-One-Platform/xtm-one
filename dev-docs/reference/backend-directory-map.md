# Backend directory map

Map of `backend/` and the request / assignment execution flow. Rules for working in the backend live in `backend/AGENTS.md`; each area directory (`api/`, `services/`, `integrations/`, `tools/`, `llm/`, `channels/`, `models/`, `agents/`) has its own `AGENTS.md` pointing to the reference docs that list its modules. `core/` and the top level are listed in full; `api/v1/` and `services/` list a selection.

## Directory Map

```
backend/
├── app/
│   ├── main.py              ← FastAPI app entry: lifespan (background migrations + seeding, the assignment scheduler loop), router registration
│   ├── worker.py            ← SAQ worker: background jobs + the orchestration reconciler task
│   ├── healthcheck.py       ← Process health checks (`python -m app.healthcheck <check>`)
│   ├── core/
│   │   ├── _pre_squash_revisions.py ← Frozen migration-squash compatibility metadata
│   │   ├── ann_index_builder.py ← Post-startup background builder for deferred (HNSW) ANN indexes
│   │   ├── base_path.py     ← Serving the platform under a URL path prefix
│   │   ├── bounded_receive.py ← A request body cut off past a byte limit while the form parser reads it
│   │   ├── cancellation.py  ← Cancellation-resilient cleanup (`uncancel_during_cleanup`, `run_cleanup_to_completion`, `run_to_completion`)
│   │   ├── color.py         ← Pure-stdlib colour maths (parse_hex_color, format_hex, srgb_to_lab, delta_e, chroma)
│   │   ├── config.py        ← Settings from env vars (Pydantic BaseSettings)
│   │   ├── constants.py     ← Shared constants of services and API routes
│   │   ├── cpu_time.py      ← What a budget on CPU work is charged: CPU time, never load
│   │   ├── csp.py           ← Content-Security-Policy on every HTML document: report-only / enforce / off, the SPA policy, per-page policies (`register_page_policy`), inline scripts allowed by hash
│   │   ├── database.py      ← Async engine, session factory, Base class; JSON columns written through `json_serializer` (NUL / lone surrogates sanitized by `sanitize_json_strings`, known secrets redacted)
│   │   ├── db_availability.py ← Getting through a short PostgreSQL outage without a flood of 500s
│   │   ├── destination_policy.py ← Which network addresses a backend-initiated request may reach
│   │   ├── egress.py        ← The one choke point for the platform's outbound HTTP
│   │   ├── email_domains.py ← Allowed email domains: which addresses may get a new account
│   │   ├── executor.py      ← Process-wide default ThreadPoolExecutor of the event loop
│   │   ├── feature_flags.py ← Env-var-driven platform feature flags (ENABLED_FEATURES), fail-closed default, no admin UI
│   │   ├── hot_imports.py   ← ensure_hot_path_imports(): thread-offloaded preload of heavy first-use imports
│   │   ├── http_body.py     ← Reading a request body under a size bound
│   │   ├── index_maintenance.py ← Idempotent, column-set-guarded reconciliation: creates any model-declared index missing from the live DB
│   │   ├── json_sql.py      ← A `json` value PostgreSQL stores but cannot parse: `json_unparseable()` / `when_json_parseable()` guard every `::jsonb` cast of a `json` column, `jsonb_cast_indexed()` for the columns an index keeps clean
│   │   ├── jwks.py          ← Shared JWKS client cache (one PyJWKClient per URL)
│   │   ├── launch_marker.py ← Whether this launch of the web server already migrated and seeded the database
│   │   ├── log_once.py      ← log_once() / log_once_soon(): an operator notice said once per interval for the whole platform (Redis SET NX EX keyed by release and `fingerprint()`, fail-open)
│   │   ├── logging.py       ← Structured logging configuration
│   │   ├── lone_surrogates.py ← A lone UTF-16 surrogate refused where it enters an assignment or a flow: lone_surrogate_error(), escape_surrogates()
│   │   ├── loop_watchdog.py ← Event-loop stall watchdog: silent hangs become actionable logs
│   │   ├── metrics.py       ← Idempotent Prometheus collector registration (`get_or_create_counter`)
│   │   ├── migration_ddl.py ← Lock-safety helpers for migration bodies (`terminate_lock_holders`, `set_lock_timeout`, `create_index_concurrently`)
│   │   ├── migrations.py    ← Alembic runner + fresh/existing-DB convergence (create_all + stamp head, schema extras, index reconciliation)
│   │   ├── parse_offload.py ← Bounded thread pool for CPU-bound document parsing
│   │   ├── pinned_request.py ← Send a request to the address the destination policy checked, not to a name
│   │   ├── pinned_transport.py ← httpx transport connecting only to addresses a policy admits
│   │   ├── platform_identity.py ← Who a registered platform is and where XTM One reaches it
│   │   ├── proxy_headers.py ← Which reverse proxy may speak for the client (startup report, uvicorn trust list)
│   │   ├── queue.py         ← Shared SAQ queue helpers for web and worker processes
│   │   ├── rate_limit.py    ← Fixed-window rate limiting backed by Redis
│   │   ├── redaction.py     ← Shared credential redaction
│   │   ├── redis.py         ← Redis connection layer (standalone or Sentinel)
│   │   ├── redis_key_prefix.py ← REDIS_KEY_PREFIX: the client classes that prefix keys and channels
│   │   ├── registration_token.py ← The platform registration token: the one place it is read and compared
│   │   ├── schema_readiness.py ← Holds the SAQ worker until the database schema has caught up with its code
│   │   ├── scopes.py        ← The three scope vocabularies a flow node can offer
│   │   ├── secret_refs.py   ← Vault references: a secret field pointing at an enterprise vault instead of holding the value
│   │   ├── security.py      ← JWT, password hashing, Fernet encrypt/decrypt, webhook signature verification, `timing_safe_str_eq` (constant-time comparison of a request-derived str), `timing_safe_header_eq` (a secret sent verbatim in a header), `configured_secret` (a secret read out of a JSON config)
│   │   ├── security_controls.py ← Every security switch registered once and reported by the Security Audit with its state; see dev-docs/reference/backend-api-security-controls.md
│   │   ├── security_options.py ← Security options: the environment variable first, then Settings > Security
│   │   ├── session_transactions.py ← Where the transactions of one Session sit relative to each other (after-commit work tagged with the innermost transaction)
│   │   ├── sessions.py      ← Web session absolute lifetime + idle timeout: `auth_time` / `sid` claims (a re-mint passes `continues=` to `create_access_token`), `verify_session()` on every surface accepting a session JWT (the web chat's open streams rechecked with `recheck_session()`), idle deadline in Redis written only by `record_activity()` (`POST /auth/session/activity`), fail-open without Redis
│   │   ├── sso_helpers.py   ← SSO configuration helpers (OIDC/SAML)
│   │   ├── startup_checks.py ← Pre-flight dependency checks with retry
│   │   ├── startup_locks.py ← The database lock identities of the platform startup (shared by migrations and the watchdogs)
│   │   ├── startup_watchdog.py ← A startup that never completes becomes an ERROR naming what holds it
│   │   ├── totp.py          ← The one RFC 6238 implementation: browser profile sign-ins, a TOTP seed read from a vault, the 1Password integration's code tool
│   │   ├── undeclared_proxy.py ← A reverse proxy whose client address the audit trail cannot read
│   │   ├── url_identity.py  ← The identity an http(s) URL names, compared the RFC 3986 way
│   │   ├── utils.py         ← Shared helpers (slugify, validate_slug, parse_delay, markdown_to_slack, safe_uuid, is_valid_uuid, normalize_github_repo, cron validation, secret masking, client address)
│   │   ├── worker_process.py ← What a worker process started for CPU work needs from its first moment
│   │   └── xtm_auth.py      ← Cross-platform Ed25519 JWKS authentication
│   ├── api/                 ← FastAPI routers (see api/AGENTS.md; routers in dev-docs/reference/backend-api-routes-detail.md)
│   │   ├── deps.py          ← Shared dependencies. `get_current_user_ungated` authenticates (JWT / `fcp-` API key / trusted platform JWT); its session-JWT branch `resolve_session_jwt` holds the one set of session rules (confined tokens, deactivated accounts, tokens older than the last password change, `core/sessions.py` lifetime and idle timeout), also called by a surface reading its own token (`/activity/stream`) - never re-derive them; `get_current_user` = that plus the platform-wide Terms of Use gate (`enforce_terms_of_use`), what every endpoint depends on (`require_admin` and the rest chain through it). Exemption is by dependency choice, not a path allow-list.
│   │   ├── oauth_callbacks.py ← OAuth redirect handler
│   │   ├── auth_sso.py      ← OIDC + SAML SSO flows (with automatic group mapping)
│   │   └── v1/              ← One router module per area (selection below; every router registers in main.py)
│   │       ├── helpers.py   ← Shared DB lookups, user_visible_filter/query, company-managed toggle
│   │       ├── agents.py    ← Agent CRUD + company-managed + disable_chat toggle + format-aware export/import
│   │       ├── agent_import.py ← Shared agent-import pipeline (helper module, not a router) of both import endpoints: agent + tools + assignments, initial persona/config versions, DEFAULT_BUILTIN_TOOL_IDS
│   │       ├── agent_templates.py ← Agent template CRUD (admin)
│   │       ├── agent_chats.py ← Agent conversations across all channels (Chats tab)
│   │       ├── annotations.py ← Ratings of agent work: the caller's thumbs + comment + criterion scores on a chat message, assignment run, background task or delegation (each target's own read rule); `GET /mine`; an agent's feedback list + summary for its managers
│   │       ├── improvement_proposals.py ← Improvement proposals: list per target / get (the target's managers), `POST /persona` (admins, `EVALUATION`), approve / reject (the target's managers); writes AI-gated
│   │       ├── feedback_criteria.py ← Settings > Quality: feedback criterion CRUD (reads for every user, writes admin-only and audited)
│   │       ├── agent_memory.py ← Agent memory list + semantic search
│   │       ├── agent_skills.py ← Agent skill CRUD + refresh from URL (public or private GitHub)
│   │       ├── prompts.py   ← Prompt CRUD + company-managed toggle
│   │       ├── flows.py     ← Visual flow builder: Flow CRUD, node catalog, dry graph validation, version history + restore, native/LangFlow/n8n import + export. `status = active` is refused on PUT: activation is the endpoint in `flow_runs.py`
│   │       ├── flow_runs.py ← Flow execution: activate/deactivate (mints the webhook secret, returned once), manual run (`POST /flows/{id}/run`, drafts included), run history with per-node steps, cross-flow listing (`GET /flows/runs/all`), cancel
│   │       ├── assignments.py ← Assignment CRUD + trigger + runs + prompt transparency + KB read/write
│   │       ├── webhooks.py  ← External webhook receiver (`/api/hooks/...`)
│   │       ├── knowledge_bases.py ← KB CRUD + documents + sync sources + company-managed
│   │       ├── integrations.py ← Integration CRUD + OAuth + company-managed
│   │       ├── integration_catalog.py ← Provider catalog data (PROVIDER_CATALOG)
│   │       ├── integration_openapi.py ← OpenAPI / Swagger integrations: spec preview (URL, file text or ZIP, JSON or YAML), detail, operation selection, refresh, replace, client certificate, device-code poll, implicit sign-in token intake; creation is `POST /integrations` with an `openapi` block
│   │       ├── integration_elements.py ← Selectable elements (Teams channels, Slack channels, GitHub repos)
│   │       ├── integration_notifications.py ← MS Graph + Slack notification handlers (sender authenticated by `services/integration_notification_auth.py`, Slack messages dispatched by `services/slack_events.py`)
│   │       ├── integration_testing.py ← Integration connectivity test helpers (helper module, not a router)
│   │       ├── binding_access.py ← Who may add a binding (MCP servers, integrations, channel bots, a trigger's integration) to an agent or an assignment: the save-time check
│   │       ├── chat.py      ← Conversations + messages (the REST send) + the web chat's turn runner, `run_chat_connection` behind the `ChatConnection` protocol, served over SSE by `chat_web_stream.py`
│   │       ├── chat_commands.py ← Chat slash commands (`services/chat_commands/`): the list, and `/context` / `/help` answered for whoever may send in the conversation
│   │       ├── chat_web_stream.py ← The web chat over HTTP + SSE (`/api/v1/web`, the embedded chat's relative `/chat/...` paths) for `@filigran/chatbot`: send streams, steer / approve / cancel / skip-wait, the events stream, sessions
│   │       ├── chat_export.py ← A conversation as Markdown or PDF (`services/conversation_export.py`): readable by whoever may read the conversation, chart blocks written as tables
│   │       ├── public_chat.py ← Public chat widget (web-embedded agents)
│   │       ├── public_catalog.py ← Filigran AI Catalog (browse + clone published entities)
│   │       ├── mcp_servers.py ← MCP server CRUD + test + company-managed
│   │       ├── mcp_endpoint.py ← MCP endpoint (global + user agent exposure via Streamable HTTP)
│   │       ├── mcp_auth.py  ← The one authentication of the MCP endpoints taking a user's credentials (`/mcp/user`, `/mcp/xtm`, the Platform MCP Servers): `authenticate_user()` (an `/mcp/oauth` token for the resource hit, else an `fcp-` key for the MCP surface), `try_oauth_token()` (also used by `/mcp/global`), `raise_unauthorized()` (401 + RFC 9728 `WWW-Authenticate`); kept out of the Enterprise Edition `mcp_endpoint.py`, which no Platform MCP Server file imports (direct imports only)
│   │       ├── mcp_transport.py ← The one MCP Streamable HTTP transport of the MCP endpoints: 2026-07-28 requests served statelessly and validated, handshake revisions (2025-11-25 / 2025-06-18 / 2025-03-26) via `initialize`, `Origin` validation, tool errors in-band; `is_modern_request()`
│   │       ├── mcp_platform.py ← Platform MCP servers (OpenCTI / OpenAEV / OpenCRQ business tools)
│   │       ├── mcp_xtm.py   ← XTM One MCP server (this platform's own tools: traces, agents, KBs, prompts, skills)
│   │       ├── mcp_oauth.py ← MCP OAuth 2.1 authorization server
│   │       ├── custom_tools.py ← Custom tool CRUD + test execution + company-managed
│   │       ├── channel_bots.py ← Channel bot CRUD + catalog + tools + company-managed (Slack, Teams, WhatsApp, Telegram, Discord, Google Chat)
│   │       ├── agent_accounts.py ← Agent accounts: owner-only CRUD, rule simulator, pickers, approval queue, activity log, replies and analytics, public push endpoint (`notification_router`); contract `dev-docs/reference/agent-accounts.md`
│   │       ├── settings.py  ← LLM provider CRUD, general assistant config, user prefs, SSO
│   │       ├── api_keys.py  ← User API key CRUD (for the LLM proxy)
│   │       ├── llm_proxy.py ← OpenAI-compatible LLM proxy endpoint
│   │       ├── users.py     ← User management (admin)
│   │       ├── groups.py    ← Group CRUD + membership (admin)
│   │       ├── audit_logs.py ← Audit trail (admin, searchable)
│   │       ├── panic.py     ← Emergency stop: stop, stop reason, resume (mandatory reason), status (self scope for everyone, platform scope admin-only), the emergency API token (admin), `POST /panic/emergency` (bearer `xes-…` token, platform stop); never gated by ai_enabled
│   │       ├── hooks.py     ← Platform event hook registration (admin)
│   │       ├── xtm_hub_registration.py ← Registration with the XTM Hub (admin; coarse status for the dashboard pill)
│   │       ├── security_audit.py ← Security configuration audit (admin), including the CSP mode, the last 7 days of violations and who may frame the platform
│   │       ├── csp_reports.py ← Browser-sent CSP violation reports: unauthenticated, rate limited, body-capped; processing in services/csp_reports.py
│   │       ├── tasks.py     ← Task history
│   │       ├── activity.py  ← Activity feed + SSE stream
│   │       ├── learning.py  ← Document ingestion + style learning
│   │       ├── ai.py        ← AI utilities
│   │       ├── ai_adoption.py ← AI Adoption dashboard (agentic AI only): overview, gaps
│   │       ├── auth.py      ← Register, login, me, auth config, Terms of Use state/accept/decline (platform-wide, admin-only accept; these routes + `/me` use `get_current_user_ungated`)
│   │       ├── invitations.py ← Admin-generated, email-bound, single-use invitation links
│   │       ├── intents.py   ← Intent + vertical CRUD and agent bindings (AI capability routing)
│   │       ├── variables.py ← Variable CRUD (encrypted key-value pairs for assignments)
│   │       ├── datasets.py  ← Evaluation dataset CRUD
│   │       ├── evaluations.py ← Agent evaluation runs and scoring
│   │       ├── traces.py    ← Native LLM trace storage API (list, detail, stats)
│   │       ├── work_items.py ← Durable work-item queue
│   │       ├── shared_state.py ← Cross-run shared state store
│   │       ├── presentations.py ← Slide decks: read, edit (`PATCH`: content through the same `validate_spec` as the agent tools, revision-checked, 409 on a stale `expected_revision`; sharing), import, duplicate, public link, comments, delete
│   │       ├── video_studio.py ← Video Studio (behind the `VIDEO_STUDIO` flag): projects, sharing, quality check, renders, public link, templates, takes, clips, music, files, confined media URLs
│   │       ├── video_public.py ← A video's public link (`/api/v1/public/videos/{token}`): no auth, the token is the credential; the current render's files only, one 404 for every refusal
│   │       ├── browser_profiles.py ← Browser profiles: CRUD, sign-in, session check, sign-out, MFA; not behind the Video Studio flag
│   │       ├── template_containers.py ← Template container CRUD + structured (non-vectorized) filtering + asset management
│   │       ├── ctem_metrics.py ← CTEM metrics (continuous threat exposure management)
│   │       ├── platform.py  ← External platform registration + embedded chat proxy (OpenCTI / OpenAEV)
│   │       ├── platform_access_notices.py ← `POST /platform/access-notices`: a registered platform has XTM One email a user it gave access
│   │       ├── extension.py ← Browser extension execute-task proxy
│   │       ├── admin_objects.py ← Admin object browser (admin); `ENTITY_MAP` / `objects_union()` is also what the `platform_insights` tools count
│   │       ├── metrics.py   ← Prometheus metrics endpoint (token-protected)
│   │       ├── xtm_auth.py  ← JWKS public keys for cross-platform JWT verification
│   │       ├── integration_github_webhooks.py ← GitHub webhook receiver
│   │       ├── graph_pagination.py ← Shared graph/connection pagination helper
│   │       ├── platform_settings_helpers.py ← PlatformSettings load/update helpers
│   │       ├── versioning_helpers.py ← Entity version snapshot/rollback helpers
│   │       └── schemas/     ← Pydantic request/response schemas, one module per area
│   ├── services/            ← Business logic (see services/AGENTS.md and dev-docs/reference/backend-services-*.md; selection below)
│   │   ├── audit.py         ← Audit logging service (log_audit helper)
│   │   ├── chat_web_transport.py ← The web chat's SSE transport: `HeadlessTurnConnection` (one send, injected Stop / skip-wait), `DetachedTurnStreamResponse` (raw ASGI: a client gone only detaches, the graceful drain still cuts the turn), the per-process registry of streams running a turn
│   │   ├── csp_reports.py   ← CSP violation reports: both formats parsed, URLs scrubbed, summary logging, `csp_violation_reports_total`, a per-directive per-UTC-day count shared by every replica (Redis hash `csp:violations:<YYYYMMDD>`, fail-open) read by `recent_violations()`; reports are never stored
│   │   ├── registered_platform_origins.py ← Origins of active registered platforms allowed as CSP `frame-ancestors` (per-process snapshot, refreshed on registration changes and every 5 minutes)
│   │   ├── agentschema/     ← Microsoft AgentSchema import/export format (vendored v1.0 JSON Schemas, safe YAML/JSON parsing, auto-detection, validation, field mapping; see dev-docs/agentschema-format.md)
│   │   ├── builtin_tools/   ← Builtin tool builders package (code interpreter, image gen, KB search, template search, memory search, handover, agent builder, skills, tool filtering, message serialization); `__init__.py` re-exports the full surface
│   │   ├── tool_classification.py ← The one read/write classifier of tool names (`classify_tool_name()`, `infer_tool_type()`), read by the integration catalog, the agent loop's dedup cache and loop breaker, and the MCP tool annotations; standard library only
│   │   ├── mcp_tool_metadata.py ← What XTM One's MCP servers tell a client about each tool: `tool_to_mcp()` (the `tools/list` serializer), `title` + `annotations` from the name, `agent_tool_name()`; pinned by `tests/test_mcp_tool_metadata.py`
│   │   ├── mcp_packages/    ← Downloadable installer packages for `/mcp/xtm` (Claude Code plugin, Claude Desktop `.mcpb`, Agent Plugins); tool reference generated per caller
│   │   ├── flow_catalog.py  ← The flow node catalog: node kinds, ports, config fields, output variables; read by the validator and the builder UI (`GET /flows/node-catalog`); `capability.*` kinds wire into the agent node over the `capability` edge kind
│   │   ├── flow_graph.py    ← Canvas document schema + validation (structure, port compatibility, upstream-only `{{node_id.output}}` resolution); pure, called on every keystroke by `POST /flows/validate`
│   │   ├── flow_references.py ← Cross-flow validation (`flow.call` cycles such as A calls B calls A); needs the database, runs on write only
│   │   ├── flow_import.py   ← Flow import/export: native documents plus a best-effort LangFlow adapter (unmapped components become `unresolved` nodes; the report states what did not survive)
│   │   ├── flow_import_n8n.py ← n8n workflow import: mapped, mapped-to-review (the walker refuses to run it until cleared) or unresolved nodes; credentials and literal secrets never cross over
│   │   ├── flow_import_n8n_apps.py ← n8n app nodes → XTM One provider, operation → tool, trigger → event; CTEM verdict of the apps with no provider
│   │   ├── flow_import_n8n_scripts.py ← generated Python reproducing n8n's data nodes in a Code node
│   │   ├── flow_versioning.py ← Flow version snapshots + retention, mirroring `prompt_versioning.py`; restore writes a new version
│   │   ├── flow_runner.py   ← Flow execution: sequential graph walker + per-node executors. Freezes the graph, journals every node into `flow_run_steps` two-phase (committed `running` before the node executes), evaluates conditions deterministically, propagates taint from untrusted triggers, parks (delay / sub-flow / assignment wait) with its cursor in `flow_runs.state`. Executors reuse `run_agent_chat` (per-node model / temperature / reasoning effort / iteration / token / persona overrides, `allow_handover`), `create_work_item`, `trigger_assignment`, `build_integration_tools`, `validate_outbound_url_async`
│   │   ├── flow_run_stream.py ← Live `flow_run_update` SSE frames (ids + statuses only) after every step transition and cancel; a Redis failure trips a 30s quiet period; polling stays the floor
│   │   ├── flow_triggers.py ← Automatic flow triggers: the cron sweep (on the 60s web scheduler tick), webhook path resolution for `/api/hooks/flows/{id}/{path}`, integration-event polling; triggers run as the flow's owner, ownerless flows skip them with a warning
│   │   ├── assignment_engine.py ← Assignment execution engine: `trigger_assignment()`, run execution, per-assignment delivery queue
│   │   ├── assignment_helpers.py ← Assignment helpers (prompt building, tool setup, memory)
│   │   ├── orchestration_engine.py ← Durable orchestration engine: the FSM + transactional outbox, the one scheduling / leasing / retry / recovery contract (`create_execution`, `claim_due`, `complete` / `fail`)
│   │   ├── orchestration_reconciler.py ← The drive loop of the durable engine (runs in the worker; see How Data Flows)
│   │   ├── orchestration_adapters/ ← One adapter per execution kind (assignment, sub_agent, evaluation, flow, scheduled_work_item, autonomous_pentest, the Video Studio take / render / clip / music jobs); `browser_profiles.py` runs no execution, only the keep-alive's periodic hook
│   │   ├── custom_tool_executor.py ← Sandboxed Docker/subprocess executor for custom JS + Python tools
│   │   ├── autonomous.py    ← The scheduler tick (cron triggers, integration polling, KB sync, memory maintenance, skill refresh, housekeeping)
│   │   ├── scheduled_work_items.py ← Scheduled work items: one-off actions every agent runs at a set time, no assignment; `scheduled_work_item_runner.py` dispatches and runs them
│   │   ├── vector.py        ← pgvector CRUD (store/search embeddings)
│   │   ├── learning.py      ← RAG ingestion + style learning
│   │   ├── agent_chat.py    ← Agent-specific chat handling
│   │   ├── general_chat.py  ← General assistant chat
│   │   ├── chat_commands/   ← Chat slash commands: registry and parser, the `/context` report read off the loop's stored readings, Markdown rendering
│   │   ├── conversation_export.py ← A conversation as Markdown or PDF, behind `api/v1/chat_export.py`
│   │   ├── platform_insights/ ← The `platform_insights` suite's reads (`builtin_tools/platform_insights.py`): one module per tool over the admin pages' own read paths (`admin_objects.ENTITY_MAP`, LLM Analytics, AI Adoption, audit log), scoped by `scope.load_scope`; `projection.py` fits a monthly series
│   │   ├── departments.py ← The ONE rule of the department a person counts for: directory department, then group rules, else Unassigned
│   │   ├── general_assistant_link.py ← The agent linked to the General Assistant that answers in its place, and the rule it must meet
│   │   ├── general_assistant_skills.py ← The General Assistant's skills: `GeneralAssistantConfig.skill_ids`, NULL = the built-in skills bound to it (`effective_skill_ids()`); a linked agent's skills win
│   │   ├── platform_access_notices.py ← Who may send an access notice (a token signed by an active registration) and its link, only on the platform's public URL
│   │   ├── platform_chat_artifacts.py ← The decks, reports and videos an embedded-chat answer shows (OpenCTI / OpenAEV / OpenCRQ panels, `api/v1/platform.py`): descriptors for a panel that renders them, viewer links otherwise, reads and downloads by each artifact's own rule
│   │   ├── dashboard_layouts.py ← The widgets and grid bounds of the per-person dashboards (CTEM command center per vertical, Copilot dashboard), checked on `PUT /settings/preferences`
│   │   ├── document_processor.py ← Document processing pipeline
│   │   ├── chunking.py      ← Text chunking for embeddings
│   │   ├── code_interpreter.py ← Sandboxed Python code execution (matplotlib charts)
│   │   ├── persistent_memory.py ← Durable memory with vector search in PostgreSQL
│   │   ├── delegation.py    ← Unified inter-agent delegation tracking (consult, transfer, background task)
│   │   ├── annotations.py   ← Rating agent work: target registry (`resolve_target`), annotation upsert / delete, Langfuse forwarding, an agent's feedback behind one gate (`load_feedback_agent`), the per-criterion summary (`summarize_agent_criteria`), the hourly orphan sweep
│   │   ├── evaluation_engine.py ← Evaluation runs: executes the pending runs, scoring, headlines (`dev-docs/evaluations.md`)
│   │   ├── evaluation_continuation.py ← Asynchronous evaluation episodes: the signed resume callback and its timeout sweep
│   │   ├── evaluation_placebo.py ← Placebo-controlled evaluation: the placebo text and its execution-mode rule
│   │   ├── evaluation/      ← The evaluation module's parts: launch, settings, fingerprint, aggregation, extraction, metric_package/ (`dev-docs/reference/evaluations.md`)
│   │   ├── evaluation_comparison.py ← Two runs of one evaluation compared deterministically: per metric, candidate minus baseline and a verdict against `NOISE_MARGIN_POINTS`; `QUALITY_METRIC`, the judge score the persona loop compares
│   │   ├── improvement_proposals/ ← Improvement proposals: `lifecycle` (`file_proposal`, `approve`, `reject`, `settle` / `settle_open_proposals`, `set_proposal_status`, `approval_error`), `registry` (one handler per type, live watchers), `dedup`, `persona` (the persona handler: a new persona version, its re-evaluation, its rollback)
│   │   ├── persona_improvement.py ← A person's persona rewrite request (#1044): `baseline_problem`, `gather_evidence`, `generate_rewrite`, `rewrite_length_limit`, `propose` (files a `persona` improvement proposal)
│   │   ├── feedback_criteria.py ← Feedback criteria rules: definition shapes, weights, score validation, normalization (`normalized_value`), weighted scores, histograms
│   │   ├── sub_agent.py     ← Sub-agent background task management
│   │   ├── sub_agent_files.py ← How a background task's produced files reach its parent (FileContext, wake turn, attached message)
│   │   ├── skill_source.py  ← Authenticated skill sources (SKILL.md in a private GitHub repo)
│   │   ├── presentation_spec.py ← PresentationSpec validation: the single authority on what an agent-generated deck may contain
│   │   ├── presentation_legacy.py ← Retired v1 slide layouts upgraded to v2 on read and write, no data migration
│   │   ├── presentations.py ← Deck persistence (create / rewrite-in-place + revision), owner-scoped edit lookup, template theme resolution
│   │   ├── share_principals.py ← The people and groups a share grant names, as an agent tool reads them (`resolve_principals()`, `plan_grants()`, `load_principals()`); writes nothing
│   │   ├── presentation_sharing.py ← Who can open a deck: `apply_sharing()`, behind `PATCH /presentations/{id}` and the `share_presentation` tool
│   │   ├── presentation_public_links.py ← A deck's public link: token, enable / regenerate / disable, the admin switch, what `/public/presentations/{token}` serves
│   │   ├── template_container_seed.py ← Startup sync of the built-in brand decks (Filigran Brand Deck and THREAD Conference Deck in Copilot mode only, XTM One Brand Deck in every mode) and video templates (Filigran / THREAD in Copilot mode, XTM One everywhere, each wearing its deck's palette); a built-in the mode does not ship is removed with its assets
│   │   ├── browser_profiles/ ← Browser profiles (`dev-docs/browser-profiles.md`), opened by every browser of the platform: the profile browser and its URL policy (`browser`), the ONE reader and writer of a profile's encrypted credentials and saved session (`profiles`, which reads a credential held in a secret vault through the one resolver), sign-in with TOTP (`core/totp.py`) or interactive MFA on first-party forms and hosted IdPs, and the session probe of the start URL that decides its status, never its cookies (`signin`), the worker's keep-alive (`keepalive`), which profiles a run's `browser` tool may open (`agent_access`) and the signed-in session it opens, written back by compare-and-set (`agent_sessions`)
│   │   ├── video_studio/    ← Video Studio (`dev-docs/video-studio.md`): recordings in the backend browser, signed in with a browser profile (`capture`), acting on a graph canvas's points and nodes, a node found by its label (`pointer`, `node_locate`, `canvas_ocr`), the spec (`spec`) and video templates (`templates`: authored spec stored, effective spec applied on save and render), the planner (`timeline`), the compositor and ffmpeg (`compositor*`, `encoder`), the editor's live preview (`preview`), voice-over, AI clips and music (`voice`, `clips`, `music`, built-in scores in `score`), the pronunciation lexicon and its checks (`lexicon`, `lexicon_store`, `phonetics`, `pronunciation_check`), the saved revision's quality check (`quality`; a shot's frames against the video's references, `reference_drift`), jobs (`jobs`, `runners`, `render`), each render in a process of its own under a memory ceiling and a stall detector (`render_process`, `resources`) that renews its lease from the frames it draws (`lease`), confined media URLs (`media`), the public link (`public_links`), the media retention (`retention`), the render retention (`render_retention`) and the render cleanup (`render_cleanup`)
│   │   ├── template_containers.py ← Structured template lookup: the filter semantics shared by the REST API and the search_templates tool; no embedding code
│   │   ├── kb_sync.py       ← Knowledge base sync from external sources
│   │   ├── kb_sync_scheduler.py ← KB sync scheduling
│   │   ├── integration_tools.py ← Integration tool wrappers
│   │   ├── declarative_http_executor.py ← The runtime for data-defined HTTP tools (custom bundles + OpenAPI integrations): an `execution` spec places each argument and the credential; `validate_execution_spec()`, `make_declarative_handler()`; no generated code
│   │   ├── openapi_import/  ← Swagger 2.0 / OpenAPI 3.x to tools: `document`, `bundle`, `discovery`, `schema`, `security`, `operations` (`analyze_spec`), `service` (persistence + `effective_tools`)
│   │   ├── integration_notification_auth.py ← Sender authentication for the shared Slack / Graph notification endpoints; `verification_required()` reads the strict switch; `store_graph_subscription()` writes the encrypted `clientState`
│   │   ├── integration_credentials.py ← The one rule for using a stored OAuth token (owner only unless they consented to share it), SQL + Python forms; pinned by tests/test_integration_credential_gate.py
│   │   ├── trigger_integration_access.py ← Whether a run's acting user can see the integration its events come from: hourly warning + Security Audit finding when not (refusal is the save check in `api/v1/binding_access.py`)
│   │   ├── agent_memory.py  ← Agent memory management (Redis)
│   │   ├── conversation_memory.py ← Automatic incremental compaction with Pinned Directives + memory reconciliation auto-flush
│   │   ├── user_memory.py   ← Cross-agent user memory (Redis): the profile every agent reads, the notes each agent reads alone
│   │   ├── user_memory_entries.py ← User memory entries: scope / owner / freshness and the one read rule (`visible_to`)
│   │   ├── user_memory_maintenance.py ← Hourly user-memory sweep (TTL expiry, profile split)
│   │   ├── notify.py        ← Notification system
│   │   ├── xtm_hub_registration.py ← XTM Hub registration: token verified before it is stored, hourly heartbeat compare-and-set on `generation`, platform-id change or a key the Hub no longer matches ⇒ `needs_reregistration`; signed calls once the key is enrolled, `enroll_platform_key()`
│   │   ├── token_refresh.py ← OAuth token refresh
│   │   ├── mcp_oauth_discovery.py ← Outbound MCP OAuth onboarding: RFC 9728 → RFC 8414 discovery, RFC 7591 dynamic client registration, PKCE (`MCP_OUTBOUND_DCR_ENABLED`); the inbound side is `api/v1/mcp_oauth.py`
│   │   ├── mcp_oauth_tokens.py ← A remote MCP server's OAuth token kept fresh while a connection uses it (`StoredMCPOAuthToken`, the `httpx.Auth` hook of `build_mcp_runtime()`, refreshed through `token_refresh.current_mcp_access_token()`)
│   │   ├── mcp_oauth_clients.py ← Who a client of the inbound MCP authorization server is: Client ID Metadata Documents (`resolve_client_metadata()`, kill switch `MCP_CIMD_ENABLED`), `redirect_uri_allowed()`, `redirect_uri_matches()`
│   │   ├── event_poller.py  ← Integration event polling
│   │   ├── langfuse_tracing.py ← Langfuse tracing integration
│   │   ├── failure_causes.py ← The single "why did this agent run fail" classifier: `classify_run_failure(exc, ctx)` returns a `FailureCause` plus its determination (`structural` / `heuristic` / `unattributed`), persisted on `assignment_runs` and `llm_traces`, handed to the quota credit-back and reported per cause by Settings → Quotas
│   │   ├── file_storage.py  ← S3/MinIO file storage (upload, download, presigned URLs)
│   │   ├── web_fetch.py     ← Shared web fetching and HTML-to-text extraction
│   │   ├── browser_tool.py  ← Playwright headless browser automation
│   │   ├── event_bus.py     ← In-process event bus (publish/subscribe)
│   │   ├── hybrid_search.py ← Hybrid vector + full-text search for RAG
│   │   ├── prompt_guard.py  ← Prompt injection detection and content safety
│   │   ├── output_format.py ← Output format prompt injection (channel-aware overrides)
│   │   ├── tool_tracking.py ← Tool usage tracking (call_count / last_called_at on integrations, MCP servers, custom tools)
│   │   ├── tool_scaffolding.py ← `fold_tool_scaffolding()` / `transcript_tool_names()`: bounded removal of narrated tool-call blocks from a delivered sub-agent answer
│   │   ├── agent_accounts/  ← Agent accounts: rule engine (`rules.py`), the writer of an account's configuration (`management.py`, REST and agent tools), pipeline, listening, sync report (`problems.py`), activity log (`activity.py`), mailbox triage (`triage.py`), providers (Teams, Outlook); `dev-docs/reference/agent-accounts.md`
│   │   └── ai_metrics_collector.py ← AI adoption metrics collection (the platform's own agentic usage)
│   ├── agents/              ← Built-in agent templates, agents, CTI skills and their seed data, provisioned at startup (see agents/AGENTS.md)
│   ├── models/              ← SQLAlchemy ORM models (see models/AGENTS.md)
│   ├── channels/            ← Multi-channel messaging (see channels/AGENTS.md)
│   ├── llm/                 ← LLM provider abstraction and routing (see llm/AGENTS.md)
│   ├── tools/               ← Agentic loop + MCP client (see tools/AGENTS.md)
│   ├── integrations/        ← External service connectors + shared HTTP utilities (see integrations/AGENTS.md)
│   ├── a2a/                 ← A2A (Agent2Agent): inbound server (agent card, tasks, push notifications, grants, federation) and the outbound client (`client/`)
│   ├── enterprise/          ← Enterprise Edition licensing and platform license validation
│   ├── evaluations/         ← Built-in benchmark datasets and evaluations seeded at startup (`seed_data/`)
│   ├── evaluation_metrics/  ← The vendored metric suite: quality and performance metrics, pure (see its README.md)
│   ├── data/                ← model_prices.json (bundled model price table)
│   └── static/              ← Embeddable chat widget (`widget.js`) and its images
├── alembic/                 ← env.py, alembic.ini, script.py.mako, versions/ (migration scripts)
├── pyproject.toml           ← Dependencies (uv project); uv.lock pins them, transitive ones included
└── start-uvicorn.sh         ← Uvicorn launcher of the root platform image (migrations run in the app lifespan)
```

## How Data Flows

```
User creates Agent + Assignment (frontend)
  → POST /api/v1/agents, POST /api/v1/assignments

Assignment triggers (all call trigger_assignment()):
  Schedule: the scheduler loop in main.py (autonomous.run_autonomous_tick, every ~60s,
            once per platform via a Redis lock) checks cron
  Webhook:  POST /api/hooks/assignments/{assignment_id} (a delivery arriving while a run
            is active is queued as a `queued` run); legacy POST /api/hooks/{agent_id}/{path}
  Event:    integration pollers, MS Graph / Slack notifications
  Manual:   POST /api/v1/assignments/{id}/trigger

trigger_assignment():
  → Creates AssignmentRun (status=pending, or delayed) AND an
    orchestration_executions row with run_at (delay = future run_at).
    There is no delayed_run:* Redis key and no in-process execute call:
    the execution row is the single source of truth for scheduling,
    delay, leasing, deadlines, and crash recovery. (See dev-docs/FSM/.)

Worker reconciler (orchestration_reconciler, runs in the worker):
  → sweeps deadlines, reclaims expired leases, claims due executions at run_at
  → concurrency: a shared semaphore (max_concurrent_runs +
    max_concurrent_sub_agent_tasks) caps most adapters; an adapter may set
    OrchestrationAdapter.concurrency_limit for its own dedicated budget +
    claim accounting (autonomous_pentest uses MAX_CONCURRENT_AUTONOMOUS_RUNS)
  → fast cancel: an adapter with a long-running execute() may set
    OrchestrationAdapter.cancel_poll_seconds to get a watcher that fires
    AdapterContext.cancel_event the instant the row goes terminal out-of-band
    (operator cancel), so the in-flight agent loop (run_agent_chat) aborts
    between iterations instead of at the cycle end (autonomous_pentest uses
    AUTONOMOUS_CANCEL_POLL_SECONDS; the lease heartbeat is the backstop)
  → runs the assignment adapter under a heartbeated lease + asyncio.timeout:
    → Loads Agent persona, renders prompt with trigger payload
    → Connects MCP servers, builds tool list
    → Runs agentic loop (LLM ↔ tools)
    → Records result in AssignmentRun
    → Publishes notification to Redis pub/sub
      → SSE stream pushes to frontend
```
