# Usage telemetry

XTM One sends anonymous usage statistics to Filigran, as OpenCTI and OpenAEV do. This page lists what is sent, where and how often, and how to turn it off.

!!! note "What telemetry contains"

    Telemetry sends counts, averages, on/off flags and a few labels. The labels are:

    - the platform version, its unique identifier (a random UUID created at installation), its creation date and its platform mode;
    - the deployment tags you set in `TELEMETRY_TAGS`;
    - the edition (Enterprise or Community) and where the Enterprise Edition comes from (a Filigran license or a connected platform);
    - the catalog identifiers of the configured integrations, channel bots and LLM provider types (`github`, `slack_bot`, `openai`, ...), never the names you gave them;
    - the format of agent exports and imports (`native`, `agentschema`);
    - the day each daily value belongs to.

    No personal data, no user or agent names, no credentials, and no content of conversations, documents, knowledge bases, prompts or traces. The metrics carry no IP address; the collector, like any HTTPS server, sees the address the request comes from.

## Purpose of the telemetry

Filigran uses the data to:

- improve the features and performance of the platform;
- understand how the platform is used, to improve the user experience;
- produce aggregated statistics for internal and external reporting.

## How data is sent

- **Destination**: `https://telemetry.one.filigran.io/v1/metrics`, using OTLP (OpenTelemetry Protocol) over HTTPS. `TELEMETRY_ENDPOINT` points it at another collector, your own for example.
- **Frequency**: one export per installation every 6 hours, whatever the number of processes and replicas. The first one happens within a few minutes of the platform starting, unless an export already went out in the last 6 hours. The values are read from the database just before each export.
- **Startup check**: when the platform starts, it sends a `POST` of an empty JSON object (`{}`, `Content-Type: application/json`) to the destination, with no metric in it. If the destination cannot be reached or does not answer HTTP 200, telemetry stays off until the next restart.
- **Development and CI**: with `DEBUG=true` or `PLATFORM_MODE=dev`, metrics go every 3 hours to Filigran's staging collector, or to `TELEMETRY_ENDPOINT` when it is set. Inside a CI/CD pipeline (a `CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, ... variable is set), nothing is sent.

## How to disable telemetry

Set `TELEMETRY_ENABLED=false` on the platform and restart it. With Docker Compose, put it in your `.env`: the shipped `docker-compose.yml` passes it to the platform and the worker. The platform then makes no connection to the collector at all: no startup check, no export. The startup log says `[TELEMETRY] Disabled by TELEMETRY_ENABLED=false`.

Blocking outbound access to `telemetry.one.filigran.io` and restarting has the same effect. Air-gapped platforms never send telemetry.

## Telemetry metrics

Every metric name starts with `xtm_one_`. A daily metric (`_by_day`) has one value per day over the last 30 days, each labelled with its `date`. The list is exhaustive: the test suite fails when a metric is added without being listed here.

### Platform and edition

| Metric | What it reports |
|---|---|
| `xtm_one_platform_info` | Always 1, labelled with the platform mode, the edition, where the Enterprise Edition comes from and whether AI is on |
| `xtm_one_is_enterprise_edition` | 1 when AI is licensed (a Filigran license or a connected Enterprise Edition platform), 0 otherwise |
| `xtm_one_ai_enabled` | 1 when AI is switched on, 0 when it is off |
| `xtm_one_terms_accepted` | 1 when the current Terms of Use version is accepted, 0 otherwise |
| `xtm_one_registered_platforms_count` | Active registered OpenCTI, OpenAEV and OpenCRQ platforms |
| `xtm_one_registered_ee_platforms_count` | Active registered platforms with an Enterprise Edition contract |

### Content inventory

| Metric | What it reports |
|---|---|
| `xtm_one_users_count` | Users |
| `xtm_one_active_users_count` | Active users |
| `xtm_one_groups_count` | Groups |
| `xtm_one_agents_count` | Agents |
| `xtm_one_company_managed_agents_count` | Company-managed agents |
| `xtm_one_assignments_count` | Assignments |
| `xtm_one_active_assignments_count` | Enabled assignments |
| `xtm_one_integrations_count` | Integrations |
| `xtm_one_integrations_by_provider` | Integrations per catalog provider (`github`, `slack`, `opencti`, ...) |
| `xtm_one_channel_bots_count` | Channel bots |
| `xtm_one_channel_bots_by_provider` | Channel bots per messaging platform (`slack_bot`, `teams_bot`, ...) |
| `xtm_one_knowledge_bases_count` | Knowledge bases |
| `xtm_one_mcp_servers_count` | MCP servers |
| `xtm_one_custom_tools_count` | Custom tools |
| `xtm_one_prompts_count` | Prompts |
| `xtm_one_agent_skills_count` | Agent skills |
| `xtm_one_llm_providers_count` | Configured LLM providers |
| `xtm_one_llm_providers_by_type` | Configured LLM providers per provider type (`openai`, `anthropic`, ...) |

### Activity

| Metric | What it reports |
|---|---|
| `xtm_one_assignment_runs_count` | Assignment runs, since installation |
| `xtm_one_assignment_runs_by_day` | Assignment runs created per day |
| `xtm_one_assignments_ran_by_month` | Distinct assignments that ran at least once in the month (UTC), for the current and the previous month, per origin: `builtin` (seeded with a built-in agent), `template_default` (created with an agent from a template) or `custom` (written by a user) |
| `xtm_one_conversations_count` | Conversations, since installation |
| `xtm_one_conversations_by_day` | Conversations created per day |
| `xtm_one_messages_count` | Chat messages, since installation |
| `xtm_one_messages_by_day` | Chat messages sent per day |
| `xtm_one_agentic_executions_count` | Agentic executions (a chat turn, a General Assistant turn, an assignment run, a sub-agent task or a browser extension task, each counted once), since installation |
| `xtm_one_agentic_executions_by_day` | Agentic executions per day |
| `xtm_one_user_login_count` | Sign-ins (local, OIDC and SAML), since installation |
| `xtm_one_logins_by_day` | Sign-ins per day |
| `xtm_one_agent_exports_by_format` | Agent exports per target format |
| `xtm_one_agent_imports_by_format` | Successful agent imports per source format |
| `xtm_one_agent_imports_rejected_count` | Agent imports refused before anything was created |

### LLM usage

Computed from the traces the platform keeps, the same source as the Analytics page. Totals and averages only, never per user, agent or conversation.

| Metric | What it reports |
|---|---|
| `xtm_one_llm_total_cost` | LLM cost, in US dollars |
| `xtm_one_llm_total_cost_by_day` | LLM cost per day, in US dollars |
| `xtm_one_llm_total_tokens` | LLM tokens |
| `xtm_one_llm_total_tokens_by_day` | LLM tokens per day |
| `xtm_one_llm_avg_latency_ms` | Average LLM latency, in milliseconds |
| `xtm_one_llm_avg_latency_ms_by_day` | Average LLM latency per day, in milliseconds |
| `xtm_one_llm_avg_feedback` | Average user feedback score |
| `xtm_one_llm_avg_feedback_by_day` | Average user feedback score per day |

## What's next?

- [Configuration](configuration.md#usage-telemetry): `TELEMETRY_ENABLED`, `TELEMETRY_ENDPOINT` and `TELEMETRY_TAGS`
