# Platform insights

`Platform insights` is a built-in tool suite that lets an agent answer questions about the platform itself: what the AI costs, who uses it, what was created and when, how adoption evolves, who changed what. It reads the same figures as the administration pages - LLM Analytics, AI Adoption, Objects and the audit log - so what an agent tells you matches what those pages show.

Ask, for example:

- "Graph the cost of AI agents for the group Customer Success over the last 90 days."
- "What did each department spend on LLMs per month this year, and what should we budget for next year?"
- "Top five agents of Engineering, and everyone outside our seven named departments."
- "How many presentations were created each week since each release?"
- "Which models cost the most this month, and how does it compare with last month?"
- "Who deleted an agent last week?"
- "What does this platform hold today?"

The answer comes with a chart drawn in the conversation when it helps, and with the warnings the dashboards show next to the same figures.

## Who can use it

The suite is one built-in tool, `Platform insights`, chosen in the `Built-in Tools` card of an agent's configuration.

- **Administrators** read the whole platform: every user, group, agent and object.
- **Group managers** read their team's LLM analytics as well: the usage of the current members of the groups they [manage](../deployment/monitoring.md#group-managers), administrators excepted, and those groups. Ask, for example, "List the agents my team used in the last 30 days with their cost". Everything else stays their own slice, traces and sessions included.
- **Anyone else** reads their own slice only: the objects they own, the usage they generated, the groups they belong to, the audit entries of their own actions. Naming another person, or a group they are neither a member nor a manager of, is refused.

Every result says which of the three it served. When strict admin access for agent tools (`access.agent_tools_strict_admin_access`, see [Settings](../administration/settings.md)) is on, an administrator's agent reads their own slice as well.

The General Assistant carries the suite by default for administrators (Settings > General Assistant lists it, and an administrator can remove it there). It is offered to group managers' General Assistant too, serving them their team's analytics, and not to other users'; an agent that carries it serves them their own slice.

All the tools read only. They never ask for approval, and the audit log tool records each of its own reads in the audit log (once per identical read every 15 minutes), so reading who did what leaves a trace of who looked.

## The tools

| Tool | What it reads |
|---|---|
| `query_platform_analytics` | LLM usage - cost, tokens (input, output, cached), traces, errors, error rate, active users, duration, latency, feedback and cost per active user, per trace or per 1,000 tokens - as a total or per department, group, user, agent (with each agent's owner and status), model, source, day, week, month or release, split by a second dimension; with a projection, a comparison with the previous period, or as a CSV file |
| `query_platform_objects` | The objects of the platform - agents, assignments, runs, conversations, presentations, videos, reports, files, flows, fleets, workspaces, work items and more - listed, counted per type, or counted per day, week, month or release of their creation |
| `get_platform_release_history` | The builds the platform has run, with when each was first and last seen, and where the rollout of the newest stands. With no argument every recorded build comes back, oldest first; `last_n` (the newest N) and `since` (first seen at or after an ISO-8601 instant) answer newest first with a `window` naming what was served, and `running_only` answers the running build, its first boot, the previous release and the rollout alone |
| `query_ai_adoption` | The AI Adoption dashboard (agentic AI on this platform): the overview (active users, top users) and the adoption gaps (inactive and low-activity people, monthly adoption rate) |
| `list_platform_traces` | The Traces page as rows: when, which agent, model and user, source, status, failure cause, duration, tokens, cost and the start of the error message, filtered by period, users, groups, agents, models, sources, status and text, sorted like the page and 100 at most per call. A row says what a trace is, never what it holds. A run that stored its `rollup` when it closed carries it (numbers only: wall, model, tool and wait time in ms, iterations, tool calls and errors, input, output and cache tokens, tool result size), and the page adds `rollup_percentiles`: the p50 and p90 of each figure over its rows |
| `list_platform_sessions` | The Sessions page as rows: each conversation with its user, first and last activity, traces, errors, agents, models, tokens and cost, with the same filters |
| `query_audit_log` | Audit entries filtered by actor, action, entity, period and text, 200 at most per call |
| `get_platform_overview` | What the platform holds today: its identity (`instance`: the Instance ID of Settings > Parameters, `PLATFORM_ID` when forced, the platform name and its URL, answered to every caller), objects per type, agents by state, assignments by trigger, integrations by provider, knowledge base chunks, machines online, the sandbox pods per image key with how many are running, creating or pulling their image (`sandbox_pods_by_image_key`, administrators only), file storage (in total and per file source in `files_by_source`: `agent_check`, `video_take`, `agent_working`, `chat`...), the deployed build, the feature flags on, trace retention and whether the analytics aggregation is fresh |
| `query_platform_metrics` | The backend's own Prometheus series (the `xtmone_*` families the `/metrics` endpoint exports): a counter or gauge as one row per label set, a histogram as count, sum, mean and p50 / p95 estimated from its buckets. Administrators only; see [Platform metrics](#platform-metrics) |
| `query_platform_frictions` | The friction ledger counted: what agent runs struggled with (tool errors, retries, approval pauses, sandbox and runner failures, resumed turns, frictions an agent recorded itself) and whether each was disposed of. Totals over the window (recorded, occurrences with repeats, undisposed), and the same per agent, kind, class, tool or disposition and / or per day, week or month, as rows `render_chart` draws |
| `render_chart` | Draws the rows another tool returned as a chart in the answer |

### Platform AI settings

Administrators also get `get_platform_ai_settings` (a read) and `update_platform_ai_settings` (a write, gated like any other for an agent that asks before running tools). They cover the settings that bound every agent run: the wait policy, the default reasoning effort, the default chat model and its provider, and the two switches of `Settings > Access and security > Tool approvals`:

- `tool_approvals_enabled`: `false` suspends every tool approval platform-wide (**Suspend all approvals**), `true` resumes;
- `approve_tool_calls_default`: whether an agent that follows the platform default asks before running tools (**Every agent asks by default**).

Both take `true` or `false` (`null` leaves them as they are), are refused with the 403 sentence for anyone who is not an administrator, and are audited exactly as the Settings page does, marked `via: update_platform_ai_settings`. The read reports them under `tool_approvals`, with `env_seed`, the `TOOL_APPROVALS_ENABLED` value as deployed (read once when the settings row is created). Every switch an administrator changes in the UI there is readable and writable by these tools.

#### The default chat model and provider

The read reports the default under `default_model`: `agent_default_model`, `agent_default_provider`, `agent_default_provider_id`, `env_locked` (whether `DEFAULT_MODEL` pins it) and `serving_providers`, the configured providers that list the default model, the current one flagged `current`. An agent watching the providers moves the default with `update_platform_ai_settings` and `agent_default_provider` (an `id` of `serving_providers`, or a provider key that names one of them), and optionally `agent_default_model`, in a call of their own. The write goes through the handler of `Settings > AI and models > Models`: the same validation, the same audit entry (`model_defaults_changed`, each value before and after) and the same announcement rules. It is refused with its reason, nothing written, for a provider that is not configured, a provider that does not serve the model, a default pinned by `DEFAULT_MODEL` (the refusal names it) or a person who is not an administrator. The answer is the setting read back after the write, with `changed` naming each value before and after.

Every default of `Settings > AI and models > Models` is read under `default_routes`: `agent_default`, `routing`, `fallback`, `decision`, `image` and `embedding`, each with `provider_id`, `provider`, `model` and `env_managed` (the embedding route also with `dimensions`). `update_platform_ai_settings` with `default_routes` moves any of them in a call of its own, for example `{"routing": {"provider": "bedrock"}}`: a provider by id or key, an optional model (the current one by default). The checks are those of the Settings page: the provider must be configured and list the model; the embedding route changes provider only (same model and dimensions, so nothing is re-embedded); a route an environment variable pins is refused with the variable named. One refused route writes none of the others. The write goes through the Settings page handler (its audit entry with the values before and after), plus an entry naming the agent run.

Whenever a default route changes provider or model, the platform raises `platform.default_model_route.changed`, once per route, whatever made the change: `settings_page`, `agent_tool`, `provider_deleted`, `env_provider_prune` or `system`. The payload carries `route`, `before` and `after` (`provider_id`, `provider`, `model`), `cause` and `actor_user_id`, and no credential. A save that changes nothing raises nothing. The event is platform-wide, so an assignment with a `platform_event` trigger on it (filter keys `route`, `cause`, `after.provider` and the others `list_platform_events` names) runs once per change, with no need to poll `get_platform_ai_settings`.

### Purging working storage

Administrators also get `purge_platform_storage`, a write that asks for approval like any other. It applies the storage retention now instead of waiting for the hourly sweeps, with an age threshold `older_than_days` (0: everything older than one hour; the last hour is never touched):

- `agent_check`: the check files agents made to look at their own work (frames, contact sheets, insert crops, screenshots);
- `unused_recording` and `unused_shot`: the media of ready Video Studio recordings and AI shots no video uses (video, keyframes, poster, contact sheet, frames). The library entry stays, marked expired, as with the regular expiry;
- `figure_stills`: the editor's figure stills, drawn again by the next preview that needs one.

It never deletes a file a video needs, a recording or shot a video's timeline names or marked **Keep its media**, nor a video's renders, posters and captions. `dry_run` is true by default and only counts the files and bytes per category: run it first, then again with `dry_run: false`. Each call handles at most `limit` items per category (5,000 by default); `more: true` says another call finds the rest. A real purge is written to the audit log.

## Reading the figures right

The agent repeats the warnings that come with a figure. They are the same ones the dashboards show:

- **Departments never overlap; groups do.** A person counts for one department, so department rows add up to the platform total. Groups are current membership: a trace counts for the groups its user belongs to today, and a person in several groups counts in each of them, so group totals can add up to more than the platform total. Ask for org units only to leave access and distribution lists out.
- **Unattributed and ungrouped usage.** Usage nobody is behind - the platform's own calls, or a run whose agent was deleted since - and people in no group are reported apart, never dropped.
- **Missing prices.** Usage of a model with no price has no cost, and every cost total leaves it out. The result names those models; set their price in Settings > Observability > Model pricing.
- **Stale aggregation.** When the daily analytics aggregation has not run for more than 48 hours, older days may still move once it catches up.
- **Coverage start.** A window that reaches further back than the oldest data the platform still holds is flagged: the missing days are missing data, not zero activity. The daily aggregates outlive the trace retention, so a monthly series reaches further back than the traces.
- **Hidden costs.** When cost data is hidden on the platform, every cost is reported as unavailable, never as zero, and no chart is drawn of it.
- **Partial buckets.** The first and last week or month of a window cover only the part of it inside the window, and are flagged.

Every metric has every breakdown and every split, except latency (the median), which is computed as a total, per model and per day; duration, the average, has them all. A request outside that is refused with the nearest one that is computed.

## Who the usage counts for

Many traces have no user of their own: an assignment run, a sub-agent consulted by another agent, a webhook, a message on a channel. Their usage counts for the person behind them, in this order:

1. the trace's own user;
2. for an assignment run, the person who triggered it, else the identity it runs as, else the assignment's owner;
3. for a sub-agent or a background copy, the person its parent trace counts for;
4. the owner of its conversation;
5. the owner of its agent, for a webhook or a channel bot.

What none of them names is reported apart, by reason: the platform's own calls, a conversation with nobody behind it, or an agent or run deleted since. Ask for the traces' own users instead ("raw attribution") to read usage the way the Traces page does.

## Departments

Departments are the org units an administrator sets in Settings > Observability > Departments (see [Monitoring](../deployment/monitoring.md#departments)). A person's department is the one their identity provider sends as their directory department, else the first group rule they match, else `Unassigned`. It is recorded with the usage when it happens: a person who changes team brings no past usage with them.

A read can name departments, groups and people together, leave some out, or ask for everyone outside the ones named - "everyone but the seven named departments" is the `Others` bucket.

## Budgeting

- **Per head.** `Active users` counts the distinct people with usage in a bucket; cost per active user, per trace and per 1,000 tokens are computed per row.
- **Projection.** A monthly series can be projected up to 36 months ahead with a linear trend, a compound monthly growth rate or the average of the last months. The fit uses complete months only - the current month is shown as its run rate so far - and says how well it reads the past (R² and mean percentage error).
- **Comparison.** Any breakdown can be compared with the previous period of the same length, row by row.

## Exporting

Any read can be exported as a CSV file of every row, every metric as a column, without the bounds of a chart. The agent attaches the file to its answer. Administrators can also download the same CSV from `GET /api/v1/insights/analytics/export`, with the tool's arguments as query parameters, for a spreadsheet or a data warehouse load.

## Releases

The platform records each build it boots with - the version shown in the About dialog and its commit - and when it was first and last seen. A count or a cost can then be cut per release: a release is the deployed one from its first boot until the next build's.

The history starts at the first boot of a release that records it; nothing before it is backfilled. Whatever predates it is counted under `before_release_history`, and the agent says so. Analytics are kept per day, so a day on which a new build was first seen counts for that build.

The release history and the overview also say where the rollout of the newest build stands, so "is this fix deployed" is answered from the platform rather than from the deploy pipeline: **in progress** (a newer build booted and recorded itself while the pod answering still runs the previous one: its commit and the time its first pod booted are given), **draining** (the newest build answers, but the previous one was still seen minutes ago and may still be serving), **complete**, or **rolled back** (the newest recorded build has not been seen for a while and an older one answers). A deploy is known from the first boot of one of its pods: before that, nothing is recorded and the previous build is the deployed one.

## Platform metrics

`query_platform_metrics` reads the series the backend exports to Prometheus, which the deployment scrapes inside the cluster, so an administrator's agent can check them without access to that scrape: the sandbox container create times (`xtmone_sandbox_create_seconds`), the tasks a rollout orphaned (`xtmone_tasks_orphaned_total`, per build) and the resumed runs (`xtmone_run_resumes_total`, per entry, cause, graceful and build), the assignment queue gauges and every other `xtmone_*` series.

- Called without `series`, it lists the series it serves with their type and how many label sets each has.
- `labels` keeps only the rows whose labels equal the values given, for example `{"version": "f474aac6"}`.
- Only `xtmone_*` series are served. Any other name, and any PromQL expression, is refused by name.
- The view is **process-local**: the values of the one backend worker that answered, since it started. Other workers and pods keep their own counts and a restart starts them from zero, so the figures are a sample, not the cluster-wide total. The result names the host, process and build that answered (`view`) and says so in its caveats.
- Histogram percentiles are estimated from the buckets, the way Prometheus `histogram_quantile` does.
- A caller who is not an administrator gets their scope and no series.

## Charts in the conversation

`render_chart` turns query rows into a bar, stacked bar, line, area, pie or donut chart drawn in the answer, with its title and a caption. A chart holds at most 16 categories and 6 series - the same bounds as a chart in a presentation - so a 90-day window is charted per week or per month.

A chart never invents a figure: a hidden cost, or a missing value of an average, refuses the chart instead of drawing a zero.

When the conversation is exported (the chat menu's `Export as Markdown` and `Export as PDF`), or when the answer is delivered to Slack or Teams, each chart is written as a table of its values.
