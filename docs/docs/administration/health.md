# Platform Health

Platform Health (formerly the Concierge) is a company-managed fleet of agents that looks after the platform's other agents and flows. It consolidates memories, diagnoses agents and flows from their traces, ratings and run history, tries changes on sandbox copies, measures them with evaluations, and files **improvement proposals** that the people who manage the target approve. Nothing it proposes reaches production without that approval, and an applied change that regresses is rolled back.

!!! note "Built in"
    Platform Health is built in: no feature flag is needed. An administrator turns it on in **Settings > Automations > Platform Health**. The former `/concierge` page and `/api/v1/concierge` routes still answer for one minor release: the page redirects to `/health`, the API routes are deprecated aliases of `/api/v1/health` and carry a `Deprecation` header.

## The fleet

The platform ships a built-in fleet named **Platform Health**, in a company-managed workspace of the same name, with five agents:

| Agent | Role |
|---|---|
| **Platform Health** | The orchestrator. Investigates a target after a signal or a request by handing the work to the specialists, writes the weekly digest, and answers administrators in chat. |
| **Memory Steward** | Consolidates the memory of agents whose managers opted in. |
| **Quality Analyst** | Diagnoses agents and drafts persona, skill and assignment-prompt variants. |
| **Flow Lab** | Diagnoses flows and drafts repairs, optimizations and new flows on sandbox copies. |
| **Experimenter** | Measures variants with evaluations and files the proposals. |

The fleet, its agents, their assignments and its workspace are **built in and read-only for everyone**, administrators included: nobody edits, switches off, deletes, duplicates or forks them, and every attempt answers "Platform Health is built in and read-only." The platform rewrites their definition at every start. What an administrator tunes is set in Settings > Automations > Platform Health (below). None of the agents can edit an agent or a flow directly. The fleet never supervises itself.

Every piece of Platform Health work is a run of one of the fleet's assignments, and every run starts inside a **cycle**: a unit of work with its own share of the Platform Health budget. Each cycle ends with a digest.

## Settings > Automations > Platform Health

The settings are one section. A bar at its top (**On this page**) jumps to its four groups, each with a link that can be shared (`#health-settings-status`, `#health-settings-spend`, `#health-settings-checks`, `#health-settings-alerts`), then to the companion pets:

| Group | Blocks |
|---|---|
| Status and scope | Status (enable, name), scope |
| Spend limits | Budget, analysis, Module Analyst (XTM One modes) |
| Health checks | Schedule, signals, module thresholds, durations, dry-run |
| Alerts | Alert emails |

| Block | What it sets |
|---|---|
| Enable, name, scope, budget | Whether Platform Health works, the name the entry points and refusals use, which agents and flows it supervises among those their managers enrolled (all of them, or a list), and its budget per cycle and per month, in tokens and in dollars. |
| Analysis | Whether the routing model explains the findings, and its hard ceilings per day and per month, in tokens and in dollars (see [Analysis](#analysis)). |
| Schedule | When each fleet-wide cycle starts, as a cron expression in the time zone chosen: memory consolidation (default every night at 02:00), quality review (Mondays 03:00), flow review (Mondays 04:00) and the weekly digest (Mondays 07:00). Each can be switched off. A schedule never fires at start-up: it first fires at its next occurrence. |
| Signals | The thresholds below, each with its own switch. |
| Alert emails | Off by default. On, Platform Health emails the recipients named (up to 20) from the platform sender: one email per critical finding as soon as a health pass opens it, once per finding, and one digest a day (UTC) of the open findings at or above the minimum severity, sent only when there is at least one. Each of the two can be switched off. Nothing is sent while the platform email (SMTP) is not configured; no model call. |
| Durations | How long an applied proposal is watched for a regression (7 days), how long a flow that replaces an assignment runs in shadow first (3 days), how long it is watched after the switch (7 days), and how many shadow runs it needs (5). |
| Dry-run | Every write of every Platform Health agent is logged instead of executed: memory changes, proposals, sandbox copies. Digests count what was logged. |

### Spend limits locked by default

Platform Health spends outside the agentic quota: its own ceilings are the only bound on that spend. They are therefore **locked by default**. In the **Spend limits** group, the ceilings of the Budget, Analysis and Module Analyst blocks show their built-in defaults, read-only, with a lock and the variable that unlocks them; a change to one of them is refused. A value stored before the lock is kept, not applied.

To change them, set `PLATFORM_HEALTH_SPEND_LIMITS_UNLOCKED=true` in the platform's environment and restart: the stored values apply again and the ceilings are editable. The switches of the analysis and of the Module Analyst stay editable either way, since turning them off only lowers the spend. `GET /api/v1/health/settings/spend-limits` reports the lock and, for each ceiling, the value in force, its default, the stored value and its source (`default` or `database`).

### Name

The **Name** field sets the name the entry points ("Improve with …") and the refusals use. The page, the navigation entry, the Settings section, the orchestrator agent, its fleet and its workspace always read **Platform Health**: the fleet is built in, so the platform writes that name on them at every start, over a former Concierge name too. Left empty, the name is **Platform Health**. It is 1 to 40 characters, without line breaks or control characters; leading and trailing spaces are trimmed.

Saving a new name renames no agent, fleet or workspace, and the agents' personas are not changed: the name is what people read, not an instruction to the model.

A schedule that is due while the budget is reached, or while agentic AI is off, starts nothing: its last firing and the reason are shown under the schedule, and it tries again at its next occurrence.

### Companion pets

The **Companion pets** block lists the pets users may pick for their companion (see [Platform Health](../usage/health.md#the-companion)). **Butler** is built in and cannot be removed. **Add a pet** takes a pet in the Codex pet format, as one `.zip` holding `pet.json` and its spritesheet, or as the two files:

- `pet.json` with `id` (lowercase letters, digits and dashes), `displayName`, an optional `description`, `spritesheetPath` (a `.webp` or `.png` path inside the package) and, for a version 2 pet, `"spriteVersionNumber": 2`;
- a spritesheet of 8 columns of 192x208 cells: 1536x1872 for version 1 (9 rows: idle, running right, running left, waving, jumping, failed, waiting, running, review), 1536x2288 for version 2 (two more rows, the look directions).

A package of more than 8 MB, an SVG or any image that is not a single-frame PNG or WebP of the exact size, a path that leaves the package, or a field out of bounds is refused with the rule it broke. The platform keeps its own re-encoding of the spritesheet, never the uploaded file. Removing a pet shows the default pet to the users who had picked it.

## Signals

Signals are read by the scheduler **without any model call**, only for the agents and flows of the supervision scope, from figures the platform already keeps (no conversation content):

| Signal | Default threshold | Read |
|---|---|---|
| Feedback drop | An agent's weighted feedback score is 10 points lower over the last 3 days than over the 3 days before, with at least 10 ratings in the last 3 days | Every 15 minutes |
| Failure spike | More than 5 % of the last 20 finished runs of an assignment or a flow failed | Every 15 minutes, for the assignments and flows that ran since |
| Cost drift | An agent's cost per assignment run is 20 % higher over the last 7 days than over the 7 days before, with at least 10 runs in each period | Hourly, from the daily usage figures |
| Model change | The model a supervised agent resolves to changes | Hourly |

A signal starts **one investigation** on its target when it crosses its threshold. It does not start another while it stays crossed; it is ready again once the figure is back under the threshold. While an investigation, or any other cycle, is open on a target, nothing starts a second one there: the request or the signal is recorded and refused.

## Platform checks

In XTM One mode, AI switched off included, the health pass also checks the XTM One platform itself every 5 minutes. These checks read a setting or one record, call no model and need no enrolment; in Copilot mode they record nothing.

| Check | Severity | Fails when |
|---|---|---|
| Registration token | Critical | `PLATFORM_REGISTRATION_TOKEN` is the published default value, which authenticates nobody |
| Licence | Critical | A Filigran licence is installed and refused (no licence installed is Community Edition, not a finding) |
| Licence expiry | Warning | The validated Filigran licence expires within 30 days |
| Email (SMTP) | Info | No SMTP host is configured, so the platform sends no email (nor can agents, through the Platform email integration) |
| XTM Hub registration | Info | The platform is not registered with XTM Hub, or its registration lost connectivity or must be done again |
| Integration broken | Warning | A company-managed integration is in error, must be re-authorized, or failed its last connection test (one finding per integration) |
| MCP server broken | Warning | An enabled company-managed MCP server could not renew its OAuth token (one finding per server) |
| Knowledge base indexing stalled | Warning | A knowledge base has documents still processing after 1 hour (one finding per knowledge base) |
| Stale embeddings | Warning | A knowledge base was embedded with another model than the platform's embedding model (one finding per knowledge base) |
| Default model | Critical | No default model is set, so agents without a model of their own cannot run |
| Embedding model | Critical | No embedding model is set, so knowledge bases can neither index nor search |
| Quota near ceiling | Warning | A platform-wide agentic ceiling is 90 % used in its current period (one finding per period). Reading it consumes no quota |
| Dependency unreachable | Critical | Redis or the object storage does not answer its readiness probe within 4 seconds (one finding per dependency) |
| Trace backlog | Warning | Over 1,000 traces wait to be written to the trace store, so analytics and traces lag |
| No worker running | Critical | No worker says it is running, so queued runs, renders and indexing wait until one starts |
| Model failing | Warning | Over 5 % of a model's calls in the last hour failed, with at least 20 calls (one finding per model) |
| Version behind | Info, Warning | A newer XTM One release is published (info), or two or more (warning). Read once a day from the public release list; an air-gapped platform records nothing |
| Certificate expiry | Warning | The TLS certificate of the public URL (`FRONTEND_URL`) expires within 21 days or is refused (expired, untrusted, another host name). Read once a day; a URL the backend cannot reach records nothing |

A finding resolves by itself after two passes in a row without its cause; fixing or deleting the integration, server or knowledge base it names resolves it too.

## Run checks as

The agentic checks of OpenCTI, OpenAEV and OpenCRQ read each module through its integration, as one user: the **Run checks as** user. The XTM One administrator often has no access to the modules, so an administrator picks a user who has, in **Settings > Agents and automations > Platform Health > Health checks > Run checks as**, or on the **Modules** step of the guided **Set up the CTEM loop** setup. The user must reach every registered module's integration (granted to them and connected for them): otherwise the save is refused and the message names the modules the user cannot read.

There is no other identity: while no user is set, or when the user loses access to a module, no agentic check of that module runs and its card on the Platform Health page reads **Checks need a run-as user with <Module> access**, with a link to the setting. Set the user, then **Run checks now**: the card shows the checks passed, warned and failed.

## OpenCTI checks

In XTM One mode, AI switched off included, every registered OpenCTI is checked every 5 minutes, with no enrolment. The heartbeat check reads the registration; the others read the health snapshot OpenCTI sends with its registration, as long as it is under 15 minutes old. An OpenCTI release that sends no snapshot is only checked for its heartbeat. Nothing is stored while `PLATFORM_REGISTRATION_TOKEN` is the published default value.

| Check | Severity | Fails when |
|---|---|---|
| Heartbeat | Critical | OpenCTI has not registered for 15 minutes |
| Connector silent | Warning | An active connector has not reported for 30 minutes (one finding per connector) (E) |
| Connector errors | Warning | A connector has over 10 works in error (one finding per connector) (E) |
| Queue depth | Warning | Over 100,000 messages wait in the broker (E) |
| No queue consumer | Critical | Messages wait in the broker and no worker reads them |
| Search engine | Warning, Critical | The Elasticsearch or OpenSearch cluster is yellow (warning) or red (critical) |
| Search engine disk | Warning | The search engine disk is over 85 % used (E) |
| Ingestion lag | Warning | Ingestion lags by over 15 minutes (E) |

The figures marked (E) are defaults. An administrator changes them in **Settings > Agents and automations > Platform Health > Module thresholds** (XTM One modes only; Edit, then Save), or with the Platform Health settings API (`PUT /api/v1/health/settings`, keys `opencti.connector_stale_minutes`, `opencti.connector_errors_max`, `opencti.queue_depth_max`, `opencti.search_disk_max_percent`, `opencti.ingestion_lag_minutes`); the next pass, within 5 minutes, reads them.

## OpenAEV checks

In XTM One mode, AI switched off included, every registered OpenAEV is checked every 5 minutes, with no enrolment, on the same rules as OpenCTI: the heartbeat reads the registration, the others read the health snapshot OpenAEV sends with its registration while it is under 15 minutes old. An OpenAEV release that sends no snapshot is only checked for its heartbeat.

| Check | Severity | Fails when |
|---|---|---|
| Heartbeat | Critical | OpenAEV has not registered for 15 minutes |
| Healthcheck | Warning, Critical | OpenAEV reports a failing healthcheck of a type (mail server, agent or executor, security platform collector, injector contract, ...): critical when one is in error, a warning otherwise (one finding per type) |
| Integration silent | Warning | An injector, collector or executor has not reported for 60 minutes (one finding per integration) (E) |
| Agents offline | Warning | Over half of the agents are offline (E) |

The figures marked (E) are defaults, changed like the OpenCTI ones, in the same **Module thresholds** block (keys `openaev.integration_silent_minutes` and `openaev.agents_offline_max_percent`).

## OpenCRQ checks

In XTM One mode, AI switched off included, every registered OpenCRQ is checked every 5 minutes, with no enrolment, on the same rules as OpenCTI and OpenAEV: the heartbeat reads the registration, the job check reads the health snapshot OpenCRQ sends with its registration while it is under 15 minutes old. An OpenCRQ release that sends no snapshot is only checked for its heartbeat.

| Check | Severity | Fails when |
|---|---|---|
| Heartbeat | Critical | OpenCRQ has not registered for 15 minutes |
| Job failing | Warning | The last run of a background job ended in the dead-letter queue (one finding per job) |

## Agent checks

Every 15 minutes, in every mode, the health pass checks the agents enrolled in Platform Health, and them alone: after an upgrade no agent is enrolled. These checks read counts, statuses and ids, never conversation content, and call no model. With AI switched off agents do not run, so they record nothing and an open finding is kept as it was.

| Check | Severity | Fails when |
|---|---|---|
| Assignment failing | Warning | The last 3 judged runs of an enabled assignment failed |
| Schedule silent | Info | An enabled schedule has not run for 14 days |
| Cost spike | Warning | A day's tokens are over 3 times the agent's daily average |
| Owner disabled | Warning | The agent's owner account is deactivated |
| Failure spike | Warning | Over 5 % of an assignment's last 20 settled runs failed |
| Feedback drop | Warning | The agent's weighted feedback score is 10 points under the window before |
| Cost drift | Info | The cost per run is up 20 % on the window before |
| Model change | Info | The last day's runs used a model the 7 days before did not |
| Integration broken | Warning | An integration bound to the agent is in error, must be re-authorized, or failed its last connection test |
| Orphaned claims | Warning | A work item the agent owns is held by a run that ended |
| Blocked without blocker | Info | A blocked work item the agent owns names nothing it waits for |

The feedback drop, failure spike and cost drift checks read the thresholds of the [signals](#signals). A finding resolves by itself after two passes in a row without its cause; pausing or deleting the assignment, or unenrolling the agent, resolves it too.

## Analysis

The checks above are deterministic and never call a model. The **analysis** is an optional layer on top of them: one call to the platform routing model per module, which reads that module's open findings (check ids, severities, categories, subject ids and measured values; no conversation content) and writes a short explanation and the order in which to apply the fixes.

- It is **off by default**, and runs only where AI runs (licensed, switched on).
- A module is analysed only when its open findings changed, at most once a day, or when an administrator asks for it. A failed or refused analysis counts as that day's attempt: the schedule tries again the next day, and an administrator can run it at once.
- A failed model call counts its full reserved cost against the ceilings, since a provider may bill a call that timed out.
- It has a **hard ceiling** in tokens and in dollars, per day and per month. Before each call, the platform reserves the call's worst case on today and on this month; a call that would cross any ceiling is refused and the module shows *Ceiling reached*. After the call, the reservation is replaced by what the call really cost.
- The default ceilings are **100,000 tokens and 1 USD a day, 3,100,000 tokens and 31 USD a month**. They come from a measured run: one analysis of a module with 33 open findings cost 4,763 tokens; the most an analysis reads (60 findings) stays under 8,000 tokens, so the daily default covers two analyses of six modules.
- A ceiling cleared to no value refuses every analysis (*Ceiling not set*).
- Analysis calls are traced under their own source, **Platform Health**, visible in LLM analytics. They are not counted in any agentic quota or agentic execution count.
- **Analyse now does not re-run a check**: it rewrites the explanation from the findings already stored. **Run checks now** on a module card runs that module's checks again and stores new evidence. A finding from a module check made by an older version of XTM One, or whose evidence is more than a day old, is marked as possibly outdated: the analysis never recommends a rollback, a restart or any other disruptive action from it and points to **Run checks now** instead.
- After an upgrade, every registration with a finding from the previous check logic gets one new round of its module checks on its own, within the usual spend limits; until it lands, the API serves that finding with `stale: true` and the build that produced it (`check_build`).

Administrators switch the analysis on and set its four ceilings under **Settings > Automations > Platform Health > Analysis**. The **Platform Health** page shows an **Analysis** panel under the score history: today's and this month's spend against each ceiling, then each module's latest analysis (its explanation, or why it did not run: *Ceiling reached*, *Ceiling not set*, AI off, no routing model) and an **Analyse now** action, unavailable while AI is off or a ceiling is cleared.

The API is `GET /api/v1/health/analysis` (the latest analysis per module, the ceilings and the spend, and the Module Analyst's ceilings and spend under `module_analyst`) and `POST /api/v1/health/analysis/{module}/run` (an analysis now), both for administrators.

### Module Analyst spend limits

In the XTM One modes, the **Module Analyst** investigates each registered OpenCTI, OpenAEV and OpenCRQ read-only when its findings change and once a day, and attaches recommendations to the findings. It has its own ceilings, outside the agentic quota and outside the Platform Health budget above: tokens per cycle, tokens and dollars per UTC day and per calendar month. Its calls are traced as **Platform Health** and never counted in agentic quotas.

Administrators switch it on or off and set its five ceilings in the **Module Analyst** block of **Settings > Agents and automations > Platform Health > Spend limits**, next to the analysis; the block shows today's and this month's spend against them. The defaults are **80,000 tokens per cycle, 500,000 tokens and 4 USD a day, 10,000,000 tokens and 60 USD a month**: a quiet cycle measured about 22,600 tokens per module. A model call that would cross a ceiling is refused and the cycle stops with what it has written; a ceiling cleared to no value refuses every call.

## Digest

Each cycle ends with a digest built from what the cycle actually did:

- the memory consolidations (counts per operation, never the content);
- the findings, new and already known;
- the experiments, by outcome;
- the proposals filed, applied and rolled back, and how many are unmeasured;
- the budget spent (for a Module Analyst cycle, against its own ceiling);
- for a Module Analyst cycle, the recommendations it attached, by confidence, and the agentic findings of its module opened and resolved;
- everything skipped or not completed, with the reason (budget reached, a run that failed, experiments skipped).

A cycle reads **Completed** only when its run completed with nothing skipped. Otherwise it reads **Partly completed**, **Budget reached**, **Failed** or **Cancelled**. A figure that could not be read is shown as *Unavailable*, never as 0. The orchestrator's own report, written in the platform language, is shown under the figures.

The weekly digest assignment summarizes the cycles closed since the previous one, with a **Modules** part for the Module Analyst (cycles run, recommendations attached, agentic findings opened and resolved, spend against its ceiling); its result appears in the dashboard's latest results.

## The Platform Health page

Administrators find every proposal, cycle and signal on the **Platform Health** page (`/health`):

- **Improvements**: every proposal, filtered by type and status, with its measured result, the number of writes simulated during its experiment, and its evidence. Evidence the viewer may not open is counted, not linked.
- **Cycles**: every cycle and its digest; a Module Analyst cycle is listed as **Module Analyst**.
- **Signals**: the signals currently crossed, and whether each started an investigation.

Managers see the proposals and cycles of the agents and flows they manage on those pages (see [Platform Health](../usage/health.md)).

## Chat with the orchestrator

The Platform Health orchestrator answers in chat. With an administrator it reads every cycle and signal of its scope; with anyone else, only those of the agents and flows that person manages, and it may start an investigation on one of them on their behalf.
