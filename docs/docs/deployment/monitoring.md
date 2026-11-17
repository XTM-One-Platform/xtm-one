# Monitoring

This page covers how to monitor a self-hosted XTM One instance in production: health probes, metrics, request tracing, and logs. Filigran SaaS instances are monitored by Filigran.

The environment variables referenced here are documented in full in the [Configuration](configuration.md) section — this page explains what each signal tells you and how to act on it.

## Health probes

The platform exposes two unauthenticated endpoints for orchestrators and uptime checks:

| Endpoint | Use for | Returns 200 when |
|---|---|---|
| `GET /api/health` | Liveness probe | The HTTP server is running |
| `GET /api/health/ready` | Readiness probe | Database, Redis, and object storage are reachable and migrations are complete |

Use `/api/health` as a liveness probe (restart the container if it fails) and `/api/health/ready` as a readiness probe (stop routing traffic until it passes). The Docker Compose stack already wires `/api/health/ready` into its health checks so dependent services wait for full startup.

!!! tip "Kubernetes probes"
    Point `livenessProbe` at `/api/health` and `readinessProbe` at `/api/health/ready`. Readiness returns non-200 until the database, Redis, and object storage are reachable and migrations have completed, so traffic is held back until the platform is fully started.

Each web worker process (`WEB_WORKERS`) runs its own startup, and a probe is answered by whichever worker takes it. A worker can therefore be starting while its pod already receives traffic: the replacement uvicorn starts for a worker recycled after `MAX_REQUESTS`, or a second worker still starting when the first one made the pod ready. Such a worker holds the requests it receives until its startup completes, for up to 8 seconds, and only then answers `503` with `Retry-After`. It also skips the migrations and the seeding already applied since the container started, so it is serving within about a second. A seeding step the startup logged as retried "on next startup" (the built-in brand assets while the object store was unreachable, for instance) is therefore retried when the container next starts, not by a replacement worker. A worker whose startup failed answers `503` at once, and its `/api/health/ready` keeps reporting the error. If the database restarts or fails over while a worker starts, the startup waits for it to come back instead of failing: meanwhile `/api/health/ready` reports `startup_retrying` with the PostgreSQL error code when there is one (for example `database unavailable (25006)` for a read-only server, `database unavailable (ConnectionRefusedError)` otherwise), and the logs give the full error.

## Metrics

XTM One exposes Prometheus metrics on a token-protected `/metrics` endpoint.

1. Set `PROMETHEUS_ENABLED=true`.
2. Set `PROMETHEUS_BEARER_TOKEN` to a secret of at least 16 characters.
3. Scrape `/metrics` with an `Authorization: Bearer <token>` header.

!!! warning "The token is mandatory"
    When `PROMETHEUS_ENABLED=true`, the platform refuses to expose `/metrics` unless a valid `PROMETHEUS_BEARER_TOKEN` is set. This prevents an unauthenticated metrics endpoint from leaking operational data.

The endpoint serves the platform's own series, all prefixed `xtmone_`, in the Prometheus text format, together with the standard per-process metrics of the Prometheus client library (Python runtime and garbage collection).

!!! note "Read from the database, or counted per process"
    Some gauges are computed from the database at scrape time: the quota, assignment queue, relay and sandbox workspace gauges below. Every process answers the same values for them. The counters and histograms are counted in the process that did the work, and a scrape reads only the process that answers it, so read them as trends, not exact totals (see [Running more than one platform replica](installation.md#running-more-than-one-platform-replica)). The logs and the audit log are the exact record.

### Quota

| Metric | Type | Labels | What it reports |
|---|---|---|---|
| `xtmone_quota_usage` | gauge | `user_id`, `usage_type` (`agentic`, `sandbox`), `model`, `period` (`daily`, `monthly`, `yearly`), `platform_id` | Quota consumed in the current day, month and year, per user, plus a `user_id="_global"` total |

### Assignments and dispatch

The queue gauges list only the assignments with something queued, running or recently started; an assignment that goes quiet drops out. They carry the assignment's id, never its name. The queue and the variables that size it are described in [Configuration](configuration.md#webhook-delivery).

| Metric | Type | Labels | What it reports |
|---|---|---|---|
| `xtmone_assignment_queue_depth` | gauge | `assignment_id`, `platform_id` | Deliveries queued for the assignment, waiting for a free slot |
| `xtmone_assignment_queue_oldest_age_seconds` | gauge | `assignment_id`, `platform_id` | Age of the assignment's oldest queued delivery |
| `xtmone_assignment_queue_oldest_age_max_seconds` | gauge | `platform_id` | Age of the oldest queued delivery across every assignment not waiting for a human |
| `xtmone_assignment_queue_held` | gauge | `assignment_id`, `platform_id` | `1` while the assignment's queue waits for a human's answer to one of its runs |
| `xtmone_assignment_runs_active` | gauge | `assignment_id`, `platform_id` | Runs of the assignment holding a concurrency slot |
| `xtmone_assignment_concurrency_cap` | gauge | `assignment_id`, `platform_id` | How many runs of the assignment may hold a slot at once |
| `xtmone_assignment_dispatch_lag_p95_seconds` | gauge | `assignment_id`, `platform_id` | 95th percentile of the time from a run's creation to its start, over the last 15 minutes |
| `xtmone_event_to_run_seconds` | histogram | `kind` | Seconds from an event (raised by the platform, or handed over by an integration's receiver or poller) to the start of the run it triggered. `kind` is `platform_event`, `integration_event`, the assignment's trigger type for a provider webhook, or `platform_event_watch` for a one-off watch |
| `xtmone_work_item_dispatch_lag_seconds` | histogram | `path` (`tick`, `wake`) | Seconds from a work item's last activity (creation, release) to its dispatch to an inbox assignment. `wake` is the immediate path; a growing `tick` share means the Redis wake channel is not reaching the processes |

### Runs and resumes

| Metric | Type | Labels | What it counts |
|---|---|---|---|
| `xtmone_run_resumes_total` | counter | `entry`, `cause`, `graceful`, `version` | Assignment runs and background tasks resumed after an interruption (a deployment, a crash), by cause class, whether the stop was graceful, and build |
| `xtmone_tasks_orphaned_total` | counter | `version` | Running tasks whose execution lost its lease, by build |
| `xtmone_stale_run_warnings_total` | counter | `orphan_class` | Orphaned assignment runs approaching the deadline after which they are failed |
| `xtmone_stale_runs_finalized_total` | counter | `outcome` (`failed`, `completed`), `orphan_class` | Orphaned or stale assignment runs the stale-run sweep closed |
| `xtmone_turn_resume_results_total` | counter | `kind` (`replayed`, `truncated`) | Tool results an interrupted chat turn handed to the turn that resumed it |

The `version` label is the build (the short git commit, else the application version), so you can compare what a rollout cost from one release to the next.

### Sandbox workspace metrics

The code interpreter's [sandbox workspace](../usage/sandbox-workspace.md) and the sandboxes behind it report:

| Metric | Type | Labels | What it counts |
|---|---|---|---|
| `xtmone_sandbox_workspaces` | gauge | `measure` (`count`, `bytes`, `files`, `kernel_restarts`), `platform_id` | Every workspace of the platform, read from the database at scrape time. |
| `xtmone_sandbox_workspace_calls_total` | counter | `mode` (`stateless`, `session`), `workspace` (`mounted`, `unmounted`, `unavailable`) | `code_interpreter` calls. |
| `xtmone_sandbox_workspace_bytes_total` | counter | `direction` (`read`, `written`) | Bytes moved into the sandbox and saved from it. |
| `xtmone_sandbox_workspace_operations_total` | counter | `operation` (`put`, `get`, `list`, `remove`, `ingest`, `materialize_repository`, `install`, `gc_delete`), `outcome` | Workspace operations; `gc_delete` carries the sweep's reason (`idle`, `conversation_archived`, `conversation_deleted`, `run_deleted`, `orphan_objects`). |
| `xtmone_sandbox_workspace_quota_refusals_total` | counter | `operation` | Change sets refused for the quota or the file limit. |
| `xtmone_sandbox_kernel_starts_total` | counter | `reason` (`new`, `restarted`, `reset`) | `session=true` kernels started; a rising `restarted` means kernels are lost between calls (idle timeout, timeouts, calls reaching another replica). |
| `xtmone_sandbox_workspace_sync_seconds` | histogram | `phase` (`materialize`, `scan`, `upload`, `commit`) | Time moving a workspace in and out of the sandbox. |
| `xtmone_sandbox_command_stage_seconds` | histogram | `stage` (`start`, `command`, `teardown`), `outcome` | Seconds each stage of a sandbox command took. |
| `xtmone_sandbox_create_seconds` | histogram | `image_key`, `outcome` (`ok`, `timeout`, `sidecar`, `error`), `pooled` (`true`, `false`) | Seconds each OpenSandbox container create attempt took. A `timeout` series on one image key means its Pool had no warm pod (buffer taken, `poolMax` reached) or a cold node pulled the image: raise that Pool's `bufferMin`/`bufferMax` and `poolMax`. |
| `xtmone_custom_tool_sandbox` | gauge | `sandbox` (`docker`, `subprocess`, `opensandbox`, `docker_unavailable`, `opensandbox_unavailable`, `disabled`) | The sandbox custom tools actually run in: `1` for the active one, `0` for the others. |

The audit log records each file put into a workspace (`workspace_put`), each repository checkout (`materialize_repository`) and each workspace the sweep deletes, under the entity type `sandbox_workspace`. Reads change nothing and are not audited: a workspace file handed to a tool (`workspace_path`) is counted by the metrics only. The workspace of one conversation (size, last clean-up, kernel restarts, bytes moved, files) is served to its owner by `GET /api/v1/chat/conversations/{id}/sandbox-workspace`.

### Relays

Reported only while relays are enabled, labelled by the relay group's id, never its name. See [Relays](../usage/resources/relays.md).

| Metric | Type | Labels | What it reports |
|---|---|---|---|
| `xtmone_relay_group_relays` | gauge | `group_id`, `state` (`enrolled`, `online`), `platform_id` | Relays of the group: enrolled, and online now |
| `xtmone_relay_group_routed_resources` | gauge | `group_id`, `platform_id` | Integrations and MCP servers routed through the group |

### Skills and chat

| Metric | Type | Labels | What it counts |
|---|---|---|---|
| `xtmone_skill_loads_total` | counter | `outcome` (`loaded`, `already_loaded`, `not_found`, `budget_exceeded`) | Skills agents loaded during their runs. Never labelled by skill name |
| `xtmone_skill_refresh_total` | counter | `outcome` (`success`, `auth`, `not_found`, `client`, `rate_limit`, `transient`), `auth` (`authenticated`, `anonymous`) | Automatic refreshes of skills imported from a URL: tells an expired credential apart from a broken URL or a provider outage |
| `xtmone_chat_duplicate_replies_refused_total` | counter | `channel` | Chat replies refused at sending because another run had already answered every message they answer |

## Request tracing

XTM One records a trace for every LLM interaction so you can inspect what an agent did, which tools it called, how many tokens it used, and where time was spent.

A run's trace counts every token of every model call it made, including a run whose calls different providers answered (a provider failover, or a budget downgrade to another provider's model). When different models answered its calls, or their providers report usage in different formats, the run's cost is the sum of its calls' costs, as each call's own trace entry shows them; a run one model answered is priced at that model's rates.

### Native trace store

By default, traces are stored inside the platform's own database and are browsable from the application under `Traces`. The store backend is selected with `TRACING_STORE_PROVIDER`:

- `postgres` (default) — traces live in the bundled PostgreSQL. No extra service required.
- `clickhouse` — traces are read from a dedicated ClickHouse instance for higher-volume analytics. Provide the `CLICKHOUSE_*` connection settings.

To investigate an assignment, open it and select **View Traces**: the `Traces` page opens scoped to the traces of every run of that assignment, and the other filters (status, failure cause, dates, models) narrow it further. **View Traces** on a run's page scopes the list to that run alone. The scope shows as a chip above the filters; remove it, or select **Clear filters**, to see every trace again. Both shortcuts are shown to administrators, who are the ones with access to the `Traces` page.

### What a trace keeps

Two switches under `Settings → Observability` decide what the native trace store keeps, under both store backends:

| `Enable local trace storage` | `Store full payloads` | What is stored |
|---|---|---|
| On (default) | On (default) | Every trace with its payloads, as described below |
| On | Off | Every trace without what was said: the prompts, the conversation, the answers, the model's reasoning, tool arguments and tool results are dropped, and an error message keeps its first 200 characters (a provider error can quote the request it refused). Timings, token counts, cost, model, trace and tool names, status, failure cause and metadata are kept, so the `Traces` list, the analytics and the cost reports keep working |
| Off | On or off | Nothing: no trace, no span and no user feedback score is written. An administrator's own score on a trace already stored (`POST /api/v1/traces/{id}/scores`) is still recorded |

A change applies within a few seconds on every server and worker, to the traces written from then on — including a trace that was still queued when the switch changed. Traces already stored keep what they hold until the retention period deletes them; delete individual traces from the `Traces` page to remove them sooner. Deleting an agent deletes none of its traces: they stay until the retention period, listed under the agent's last name, for the people who chatted with it and for its owner (and for administrators). The switches govern the native trace store only: the [Langfuse export](#langfuse-optional), while it is enabled, still receives each call's payloads (bounded per field). Turn it off as well to keep payloads inside the platform.

While a switch is off, an agent reading traces (`inspect_my_traces`, `inspect_agent_traces`, `get_trace_payload`, and the same tools on the XTM One MCP server) is told so in the result, so it does not read a run missing from the list, or an empty input or output, as something the run did.

### Trace payload storage

While `Store full payloads` is on, trace payloads — the prompt, the conversation, the model's reasoning, tool arguments and results — are stored **whole**. They are audit material, and the trace store holds the only copy, so nothing is clipped on the way in.

Large payloads are moved out of the database rather than truncated. Above `TRACE_PAYLOAD_OFFLOAD_THRESHOLD_BYTES` (default 1 MiB), the payload is written to object storage and the trace row keeps a pointer with the exact size, a SHA-256 digest and a short excerpt. In the `Traces` UI those payloads show a **Load full payload** action that fetches the complete value on demand; the panes show a bounded preview until you ask for it, so opening a trace with megabyte-sized payloads stays fast.

Images and PDFs inside a payload are stored the same way, one object each: the payload keeps a reference with the blob's content type, byte size and SHA-256, and the span inspector lists them as **attachments** that render the actual image on demand. This is what makes the screenshot an agent received — or the chart it produced — recoverable from its trace; previously media was replaced by an `[image]` placeholder, and since the trace store holds the only copy, the picture was gone for good.

| Variable | What it controls |
|---|---|
| `TRACE_PAYLOAD_OFFLOAD_THRESHOLD_BYTES` | Size above which a payload goes to object storage (default 1 MiB). `0` disables offloading and always stores inline. |
| `TRACE_PAYLOAD_HARD_MAX_BYTES` | Inline fallback ceiling used only when object storage is unreachable (default 32 MiB). Below it the payload is stored inline anyway; above it the trace records that the payload could not be stored, so the gap is visible instead of silent. |
| `TRACE_MEDIA_OFFLOAD_ENABLED` | Whether media blobs are stored (default `true`). Set to `false` to replace them with an `[image]` placeholder instead, as earlier versions did. |

Offloading requires the same object storage as file attachments (the `S3_*` settings). Everything one trace owns — offloaded payloads and media alike — lives under a `traces/<trace_id>/` prefix. Plan disk for both volumes accordingly. Trace retention bounds how long payloads and their objects are kept — the retention purge deletes the objects along with the rows, under both store backends.

!!! note "Langfuse export"

    When Langfuse is configured, the payloads exported to it still have media replaced by placeholders and long strings truncated, regardless of `TRACE_MEDIA_OFFLOAD_ENABLED`. Langfuse ingestion has its own size limits, and its SDK would otherwise upload every blob to its own separate media storage. Only the export is reduced; the durable trace keeps everything.

### Trace retention

How long traces are kept is a platform setting, not an environment variable: set it under `Settings → Observability → Trace retention (days)` (1–365, 90 by default). It applies to both store backends, in the same maintenance cycle, and each cycle deletes the offloaded payload objects belonging to the traces it expires.

- **PostgreSQL** — expired traces are deleted in batches roughly hourly. A day's aggregated statistics (`llm_daily_stats`) are built before its raw traces are deleted and outlive them, so dashboards keep reporting on days whose traces are gone. A day whose statistics are due to be rebuilt (a repricing filled in costs they were built without) keeps its traces until that rebuild has run.
- **ClickHouse** — the same cycle sweeps the expired window: it reads the payload pointers off the expiring rows, deletes the rows, then deletes the objects they pointed at. The tables also carry a `TTL` as a backstop, re-declared whenever the retention setting changes and re-checked on each cycle, so retention still applies to tables that already exist and to a platform whose maintenance loop is not running.

!!! note "Why ClickHouse keeps a few extra days"
    The ClickHouse `TTL` is deliberately set a week beyond the retention window rather than exactly on it. Offloaded payload objects can only be found through the rows that point at them, so the rows have to outlive the object purge — a `TTL` on the exact boundary would let ClickHouse drop rows before the sweep had read their pointers, orphaning objects in the bucket. In normal operation the sweep enforces the exact window and the `TTL` never fires. If the maintenance loop stays down for longer than that margin, the `TTL` still expires the rows (retention is never silently extended) but their objects are left behind; that is a storage cost, and the sweep logs when it cannot reach object storage.

### Model usage

`Administration > Observability > Model usage` (the page was named LLM Analytics) charts what the AI costs and how it behaves over a window. Administrators read the whole platform; a [group manager](#group-managers) reads the members of the groups they manage, administrators excepted.

- **Window and filters.** The window is `24H`, `7D`, `30D` (default), `90D`, `6M`, `1Y` or `Custom` dates. Below it, the groups, agents, models and sources pickers narrow every card at once; `Clear filters` resets them. The window and the filters are kept in the URL, so a view can be shared.
- **Key figures.** Total cost (with the cost per trace), traces, tokens, average latency, error rate and average feedback, each with its change against the previous window of the same length and, for cost and traces, a daily sparkline.
- **Usage over time.** Cost or traces per day, week or month, stacked by model or as one total, with each bar's total written on it (up to 31 bars). The six largest models are drawn and the rest are grouped as `Other`; select a model in the legend to hide or show it.
- **Rankings.** `Top agents` (by cost, traces, cost per trace or average latency), `Top models` and `Top users` (by cost, traces or tokens) and `Traces by source`, each showing the top 5, 10 or 25 with their share of the total. Select an agent or a source to filter the page on it.
- **Latency.** The percentiles (P50 to P99) over the window, the median of each day, or the median and P95 of the ten busiest models.

The `Cost breakdown` and `Top errors` cards are tables: every column sorts, a search box narrows the rows, 10, 25, 50 or 100 rows show per page with the page controls above and below, and `Export CSV` downloads every row that matches the search, in the order shown. `Top errors` lists up to 500 groups of identical errors over the window, with the agent, the model, the count and when each was last seen.

The same figures are available from the `/api/v1/traces/analytics/*` endpoints: `period` takes `24h`, `7d`, `30d`, `90d`, `180d`, `1y` or `custom` (with `date_from` and `date_to`), `group_ids` narrows them to the current members of the named groups, and `GET /api/v1/traces/analytics/errors` takes a `limit` from 1 to 500 (default 20).

### Cost by agent, user, group and model

The `Cost breakdown` card splits the window's cost four ways, chosen with its `By agent` / `By user` / `By group` / `By model` switch (kept in the URL as `?breakdown=users`, `?breakdown=groups` or `?breakdown=models`, so a view can be shared):

- **By agent** — every agent that ran in the window, with its owner (the person it belongs to, or `Company-managed`), traces, cost, share of the total, average cost per trace, average latency and average feedback. The search box matches the owner's name and email too, and the CSV carries the owner's name and email.
- **By user** — the users who cost the most over the window, with their email, groups, how many agents they used, traces, tokens, average cost per trace and share of the total. The table holds up to the 500 costliest users; when more users ran traces, it says how many there were in all and counts the rest together as one `N other users` row on its last page. A deactivated account is marked `Inactive`, a deleted one `Deleted user`.
- **By group** — every group whose members ran traces, with its active users (members who ran at least one trace) out of its current members, the average cost per active user and its share of the total. Users in no group are shown apart as `Users in no group`.
- **By model** — every model that answered in the window, with its traces, input and output tokens, input and output cost, total cost, share of the total, and P50 and P95 latency.

Both views show the runs no account is behind (anonymous channel senders, public widget visitors, the `/mcp/global` key, assignment runs without a user) apart, as `No user account` on their last page, so the user view adds up to the dashboard total.

Groups are read as they are **today**: traces do not record a group, so a trace counts for the groups its user belongs to now, and moving a user to another group moves their past cost with them. A user in several groups counts in each, so the group rows can add up to more than the total; the card says so when it happens. Both views read the same per-user [daily aggregates](../administration/ai-governance.md#what-survives-the-trace-purge) as the rest of the dashboard on long windows, so they still answer after the traces are purged. The same data is available from `GET /api/v1/traces/analytics/cost-by-user` (`limit`, 1-1000, default 100; the users past it are returned together as `other_users`) and `GET /api/v1/traces/analytics/cost-by-group`, with the dashboard's filters. For a partition by org unit that never overlaps and never follows a move, use [departments](#departments).

### Departments

A group is a membership set - an access list, a distribution list, a SAML claim - and a person belongs to many. A **department** is an org unit: a person belongs to one, so department figures add up to the platform total. Departments are what the [Platform insights](../usage/platform-insights.md) analytics tool reads per department; administrators set them in `Settings → Observability → Departments`:

- **Departments.** A name, and optionally its **directory values**: the other spellings the identity provider or an HR export writes for it (`R&D` and `Research` for `Engineering`). Leave them empty when the directory writes the same name, or when the group rules alone decide. When one group per team already exists, `Create from the org-unit groups` adds one department and one group rule per group tagged `Org unit` (the group counts for the department of its name); a group rule can only be added once a department exists for it to point at.
- **Resolution order.** A person's department is resolved from the sources in the order chosen: the **directory department** - the attribute their identity provider sends at sign-in (`department` by default; the SAML attribute or OIDC claim is named in the same card), matched to a department's name or values in any case - and the **group rules**: an ordered list of groups, each counting for a department, the first one the person is a member of deciding. A person no source resolves is `Unassigned`. Usage nobody is behind (the platform's own calls) is `Unattributed`.
- **HR data.** The usual path for an HRIS field (department, team or division) is the identity provider: the HRIS provisions it into the directory, which sends it at sign-in. `Import directory departments` takes the same data as `email,department` lines from an HR export; the next sign-in that carries the attribute replaces the imported value.
- **Preview.** How many active people the saved mapping resolves, how many it leaves unassigned, and who is in each department.

The simplest setup, three steps: create the departments (or create them from the org-unit groups), add one group rule per team group with the most specific group first (`Product Management` above `Engineering`), save and `Apply the mapping to history`. The page's `How does it work?` panel and the `?` next to each field say the same in place.

The department is recorded with each trace and each daily aggregate when the usage happens, so a person who changes team never moves their past usage, and the figures survive the trace purge. When the first department is created, the usage stored until then is resolved from that mapping once, in the background (the card says how far it got). A later change of the mapping applies to new usage only; `Apply the mapping to history` resolves every stored usage again from the current mapping, and is recorded in the audit log.

The usage of a trace with no user of its own counts for the person behind it: the identity an assignment run acts as, the person a sub-agent's parent trace counts for, the conversation's owner, or the agent's owner for a webhook or a channel bot. The group's kind (`Org unit`, `Access list`, `Distribution list`, or `Not classified`, set on the group's page) lets the analytics list org units apart from access and distribution lists.

The mapping is also available from the API (administrators): `GET` and `PUT /api/v1/insights/departments`, `GET /api/v1/insights/departments/preview`, `POST /api/v1/insights/departments/directory` (`{"entries": [{"email", "department"}]}`) and `POST /api/v1/insights/departments/history`. `GET /api/v1/insights/analytics/export` returns the CSV of an analytics read - the analytics tool's arguments as query parameters - for a spreadsheet or a warehouse load.

### Models without a price

A trace of a model with no price is stored without a cost, and every cost total leaves it out. Once a day, the administrators are told in the notification bell of the models the last 7 days of traces ran on without a price, once per model. Add the price in `Settings → Observability → Model pricing` with the date it applies from, then reprice with `Only from the price's effective date` to fill the cost of the traces since that date.

### Group managers

An administrator can make a user the **manager** of a group, so they follow how the group's members use AI without becoming an administrator. Open `Administration > Users and groups > Groups`, the group, then its `Managers` tab, and pick the people in `Add a manager`. A manager does not have to be a member of the group, and a group can have several managers. Adding and removing a manager is recorded in the audit log (`bind` and `unbind` on the group, with the role `manager`).

A manager gets `Model usage` under `Administration` in the navigation, scoped to their team - the **current** members of the groups they manage, administrators excepted:

- **Model usage** shows the team's usage, with the managed groups named under the title. The groups picker lists only those groups, the `By user` and `By group` breakdowns hold only the team and those groups (a group's member count is its team's), and the CSV exports carry the same rows. The figures follow today's membership, like the group breakdown: a person who joins a managed group brings their past usage with them, and one who leaves takes it away. The manager's own usage counts only when they are a member of a group they manage.
- **Administrators are not part of a team.** An administrator who is a member of a managed group never shows in the manager's figures, breakdowns or errors; an administrator's own group filter still counts every member.
- **Traces and Sessions stay administrators' pages.** A manager does not see them in the navigation, cannot open them, and cannot open a team member's trace or session.

A manager reads the team's analytics; they do not act on it. They cannot edit the group, its members or their agents, and the `Refresh Analytics` button stays an administrator's. A user who manages no group, and is not an administrator, does not see Model usage.

Through an agent, the [Platform insights](../usage/platform-insights.md) analytics tool serves a manager the same team: analytics for any team member or managed group. The trace and session listing tools serve a manager their own traces and sessions only, like any user who is not an administrator.

The managers are also available from the API: `GET /api/v1/groups/managed` (the groups the caller manages, with the number of members their team counts), `GET` and `POST /api/v1/groups/{group_id}/managers` and `DELETE /api/v1/groups/{group_id}/managers/{user_id}` (administrators). The analytics endpoints take `group_ids` to read one or more of the caller's managed groups. `GET /api/v1/traces` and `GET /api/v1/traces/sessions` take `group_id` for administrators only.

### Langfuse (optional)

You can additionally forward traces to [Langfuse](https://langfuse.com) for external observability. Set `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, and `LANGFUSE_SECRET_KEY`. `LANGFUSE_HOST` defaults to Langfuse Cloud; point it at your self-hosted Langfuse to keep trace data on your own infrastructure.

Langfuse is complementary to the native store — enabling it does not disable native traces. Payloads sent to Langfuse *are* bounded per field, because its ingestion rejects oversized events; the native store keeps the unabridged copy. The `Enable local trace storage` and `Store full payloads` switches do not apply to the export: Langfuse receives each call's payloads for as long as it is enabled.

## Logs

The platform emits structured logs to stdout, which your container runtime or log aggregator collects.

| Variable | What it controls |
|---|---|
| `LOG_LEVEL` | Verbosity (`debug`, `info`, `warning`, `error`, `critical`) |
| `LOG_FORMAT` | `json` (structured, for aggregators) or `console` (human-readable) |

One structured `http_request` line is emitted per HTTP request on the `app.access` logger. Successful requests to `/api/health` and `/metrics` are logged at `debug` so routine probes and scrapes do not flood your logs; client errors are logged at `warning` and server errors at `error`.

In production, keep `LOG_FORMAT=json` and ship logs to a central aggregator (Loki, Elasticsearch, CloudWatch, etc.) so you can search and alert across replicas.

### Sandbox call logs

Every OpenSandbox-routed call (code interpreter, custom JS tool, custom Python tool) emits one additional structured line, `sandbox_call`, carrying CPU/RAM usage, execution time, egress policy, and outcome. It ships through the same stdout path as everything else above — no extra infrastructure required. The full field reference (names, types, units, when each is present) lives in [Configuration § Sandbox call log fields](configuration.md#sandbox-call-log-fields); this section covers how to query it once it's in Grafana.

Example line (`LOG_FORMAT=json`):

```json
{"event": "sandbox_call", "usage_type": "code_interpreter", "outcome": "success", "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "cpu_used_percentage": 12.5, "cpu_count": 1.0, "memory_used_in_mib": 64.2, "memory_total_in_mib": 128.0, "tte_ms": 842, "egress_default_action": "deny", "egress_allow_list": [], "level": "info", "logger": "app.services.sandbox_call_logging", "timestamp": "2026-07-31T12:00:00.000000Z"}
```

With logs shipped into Loki, these LogQL queries build directly on the fields above — swap `{container="platform"}` for whatever stream label your Promtail/Grafana Agent config actually attaches (container name, `job`, Kubernetes pod label, etc.):

- **All sandbox calls** (base query for a Logs panel):

  ```logql
  {container="platform"} | json | event="sandbox_call"
  ```

- **Failure rate by usage_type** (stat/alert panel — ratio of failing to total calls per interval):

  ```logql
  sum by (usage_type) (count_over_time({container="platform"} | json | event="sandbox_call" | outcome="failure" [$__interval]))
  /
  sum by (usage_type) (count_over_time({container="platform"} | json | event="sandbox_call" [$__interval]))
  ```

- **Failure breakdown by reason** (table/pie panel — `sandbox_ready_timeout`, `sandbox_create_failed`, `sandbox_create_error`, `nonzero_exit`, `execution_error`):

  ```logql
  sum by (failure_reason) (count_over_time({container="platform"} | json | event="sandbox_call" | outcome="failure" [$__interval]))
  ```

- **Average execution time (TTE) per usage_type** (time series panel):

  ```logql
  avg_over_time({container="platform"} | json | event="sandbox_call" | outcome="success" | unwrap tte_ms [5m]) by (usage_type)
  ```

- **CPU / memory usage trend** (swap `cpu_used_percentage` for `memory_used_in_mib` or `memory_total_in_mib`):

  ```logql
  avg_over_time({container="platform"} | json | event="sandbox_call" | unwrap cpu_used_percentage [5m]) by (usage_type)
  ```

- **Egress policy audit** (confirm what's actually live, per call):

  ```logql
  {container="platform"} | json | event="sandbox_call" | line_format "{{.usage_type}}: {{.egress_default_action}} allow={{.egress_allow_list}}"
  ```

CPU/RAM/TTE fields are simply absent (not `null`) on records where the sandbox never reached completion — `sandbox_ready_timeout`, `sandbox_create_failed`, and `sandbox_create_error` records have none of them. `unwrap` queries skip lines missing the unwrapped field rather than erroring, so a TTE/CPU/RAM panel naturally reflects only the calls that actually ran, without extra filtering.

## What to watch

At a minimum, alert on:

- `/api/health/ready` failing for more than a few minutes (platform not serving)
- A rising rate of `5xx` responses in the access logs
- Worker containers repeatedly restarting (background jobs and assignment runs stall)
- PostgreSQL connection saturation — see the connection-budget formula in [Configuration](configuration.md#performance)
- Disk usage on the PostgreSQL and object-storage volumes, which grow with knowledge bases and file attachments
- A rising `sandbox_call` failure rate or an unfamiliar `failure_reason` for OpenSandbox-routed code interpreter/custom tool calls — see [Sandbox call logs](#sandbox-call-logs) above
- Custom tools running outside the hardened sandbox: `xtmone_custom_tool_sandbox{sandbox=~"subprocess|docker_unavailable|opensandbox_unavailable"} == 1`. Do not alert on `sandbox!="docker"`: that also fires when custom tools are deliberately turned off (`disabled`)
- Queued assignment deliveries waiting too long — see [Alert rules](#alert-rules) below

## Alert rules

The repository ships a reference set of Prometheus alert rules for assignment dispatch: [`k8s/prometheus-rules/assignment-dispatch-alerts.yaml`](https://github.com/XTM-One-Platform/xtm-one/blob/main/k8s/prometheus-rules/assignment-dispatch-alerts.yaml). It is a `PrometheusRule` for the Prometheus operator; apply it with `kubectl apply -f k8s/prometheus-rules/assignment-dispatch-alerts.yaml`. Its `release: kube-prometheus-stack` label matches the default rule selector of a kube-prometheus-stack install: adjust it to your Prometheus's selector, or load the group into your own ruler (Mimir, Thanos).

The rules need [metrics](#metrics) enabled. They evaluate every minute and take the maximum across replicas.

| Alert | Fires when | For | Severity |
|---|---|---|---|
| `XtmOneAssignmentDispatchLag` | The oldest queued assignment delivery has waited more than 5 minutes (`xtmone_assignment_queue_oldest_age_max_seconds > 300`) | 2 minutes | `warning` |
| `XtmOneAssignmentRunStartLag` | 95% of an assignment's runs over the last 15 minutes started more than 5 minutes after their trigger (`xtmone_assignment_dispatch_lag_p95_seconds > 300`) | 10 minutes | `warning` |

Nothing is lost when they fire: a queued delivery is kept in the database, but automations answer late. To find the cause, compare `xtmone_assignment_runs_active` with `xtmone_assignment_concurrency_cap` for the assignment concerned:

- **At its cap:** the assignment receives events faster than its runs finish. Raise its `max_concurrent_runs` trigger setting, or `ASSIGNMENT_MAX_CONCURRENT_RUNS` for every assignment.
- **Below its cap:** the workers are short. Look at `MAX_CONCURRENT_RUNS`, `ASSIGNMENT_DISPATCH_MAX_PENDING_RUNS`, the number of worker replicas, or a model provider answering `429`.

The worker also logs `Assignment dispatch lag` with the three oldest queues, at most once a minute while the oldest delivery is older than `ASSIGNMENT_QUEUE_BACKLOG_SECONDS`.

## Next step

If a signal points to a problem, continue with [Troubleshooting](troubleshooting.md) for common issues and how to diagnose them.
