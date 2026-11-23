# 📊 Metrics

This page is the contract for every metric OpenCRQ exposes: every family the platform emits, under both the name it carries on each transport, with its type, unit, description and attributes. It also gives a copy-pasteable Prometheus scrape configuration, recording rules to make existing OpenCTI runtime dashboards resolve OpenCRQ's names, and the one alert condition operators should watch for pool saturation.

This page is about **your own monitoring** of an OpenCRQ deployment. It is unrelated to the [Anonymous Usage Telemetry](usage-telemetry.md) OpenCRQ sends to Filigran — that pipeline uses its own metric names, its own destination, and cannot be redirected to your monitoring stack.

## Two independent transports

Metrics are recorded once per process and served identically by two independent, opt-in transports — see [Configuration](configuration.md#observability) for every variable and default:

- **Pull (Prometheus scrape)** — `TELEMETRY__OBSERVABILITY__PROMETHEUS__ENABLED`. The process manager serves one merged scrape endpoint per container, on port `14269` by default (`TELEMETRY__OBSERVABILITY__PROMETHEUS__PORT`), covering every process the container supervises. No collector is required for this path.
- **Push (OTLP)** — `TELEMETRY__OBSERVABILITY__OTLP__METRICS__ENABLED`, sent by each process independently to the configured OTLP destination, whether that is a collector or any OTLP-native backend.

Either, both, or neither may be enabled; enabling one does not require the other.

The scrape endpoint additionally withholds itself, logging an error and leaving the rest of the platform running, in three cases: the configured port equals the healthcheck server's port, a process in the running profile is named after the process manager's own reserved name (which would collide with the `process_name` identity label), or the server fails to start. If a target that should be up is refusing connections, check the application's logs for those errors before looking at the network.

## Identity, always

Every series, on either transport, carries three identity attributes: `process.name` (the process that recorded it — a supervised role such as `http` or `worker`, or `pm` for the process manager), `service.name` and `service.version`. On the push transport a fourth, `service.instance.id`, is also present — it is omitted on the scrape transport because it changes on every process restart and would needlessly grow the series count on a target that is meant to stay stable across restarts.

### How the two encodings differ

| | Pull (scrape) | Push (OTLP) |
|:--|:--|:--|
| Instrument names | Sanitized: any character outside `[A-Za-z0-9_]` is replaced with `_`, runs of underscores collapse to one, a leading digit gets an `_` prefix, `_total` is appended to monotonic sums that do not already end in it, and the unit is **never** appended to the name (it appears in a `# UNIT` comment Prometheus itself ignores). | As declared — dotted, e.g. `db.client.connection.wait_time`. |
| Identity attributes | `process_name`, `service_name`, `service_version` as constant labels on every sample line. | `process.name`, `service.name`, `service.version` as resource attributes. |
| `service.instance.id` | Absent. | Present, as a resource attribute. |

The recorded values are identical on both transports, and so is the set of families — with one exception: `opencrq.pm.collection.ok` and `opencrq.pm.collection.failures` are scrape-only (see [Supervision and collection outcome](#supervision-and-collection-outcome) below).

Derive no name by applying the sanitisation rules above by hand: always confirm the exact scraped name against a real document, since a version bump of an auto-instrumentation can add or remove a family (guarded by the set-equality test in `back/src/utils/metrics-contract.test.ts`).

## Protection: network placement, not authentication

The scrape endpoint carries no authentication by design — it is protected by where it is reachable from, never by a credential. Never publish it outside the container or pod network; the bind address (`TELEMETRY__OBSERVABILITY__PROMETHEUS__HOST`) is the control, and **it defaults to `0.0.0.0`**, so a deployment that enables this transport without setting it listens on every interface of its container. The image declares no metric port, so nothing is exposed unless a deployment explicitly enables and routes to this transport.

An unauthenticated reader of the endpoint learns: the platform version, the process role names a deployment runs and how many of each, the database server's address, port and name (the values of `postgres.host`, `postgres.port` and `postgres.database`, which appear in the connection-pool identifier and on the query-duration histogram), Postgres `SQLSTATE` codes observed on queries, and the kinds of database operation the application issues (`SELECT`, `INSERT`, ...). Database **table names are not exposed** — the query-duration histogram carries an operation kind and, bounded, an error code, never the SQL text or the tables it touches.

## Composition across processes

Every family on this page is either per-process or owned once by the process manager. `process_name` identifies which one recorded a series: the supervised roles a profile runs (`main` in `monolith`, `http` and `worker` in `split`), plus `pm` for the process manager itself. The process manager is instrumented like any other process, so it contributes its own Node runtime series alongside the supervision families it owns — a container running `split` reports three values of `process_name`, not two.

Most per-process families are additive, and compose across processes by **sum**: a panel built with `sum(...)` is correct whether the profile runs one process or several. Two kinds of family are not additive, and `sum` is wrong for both:

- **A gauge that already *is* a statistic.** The event-loop delay families report a maximum, a mean and a 99th percentile; summing those across processes produces a number that never occurred. Aggregate `max` and `mean` by their own operator (`max`, `avg`). A percentile cannot be recombined across processes at all — the recording rules below take the worst process and say so.
- **A ratio.** `nodejs_eventloop_utilization` and `process_cpu_utilization` are bounded in `[0, 1]`; three processes at `0.5` sum to `1.5`, which is meaningless. Use `avg` for the container's average or `max` for its busiest process, and never `sum`.

When a panel spans more than one container, keep `instance` in the grouping (`sum by (instance) (...)`) unless a fleet-wide total is genuinely what you want — a bare `sum(...)` silently aggregates every scraped container together.

A future family describing state that is *shared* between the processes of a container, rather than owned independently by each, would instead aggregate by `max` at the consumer, never pre-reduced at the point of recording; no such family ships today.

## Database facts

The connection pool is **per process**: `postgres.pool_max` (default `20`, floor `10`) bounds each process's own pool independently, so a container's total connection footprint against Postgres is the sum of `postgres.pool_max` across every process that opens one. Only the supervised roles connect to the database — the process manager does not — so `split` means two pools, up to 40 connections from one container rather than 20. Size Postgres's own `max_connections` accordingly across every container the deployment runs.

**The one alert condition that matters:** connections in use at `db_client_connection_limit` **and** `db_client_connection_pending_requests` above zero, sustained across consecutive scrapes. A momentary spike is normal; a pool sitting at its ceiling with callers queued behind it, for more than one scrape interval, is saturation.

`db_client_connection_limit` carries no `db_client_connection_state` label, so the two sides have to be matched on everything *except* that label. Use `ignoring`, not `on`: an `on (...)` list that names only the pool and the process drops `job` and `instance`, and because the pool name derives from the Postgres host, port and database it is identical across replicas — so the query fails with `found duplicate series for the match group` as soon as a second container is scraped.

```promql
(
  db_client_connection_count{db_client_connection_state="used"}
    >= ignoring (db_client_connection_state) db_client_connection_limit
)
and ignoring (db_client_connection_state) (
  db_client_connection_pending_requests > 0
)
```

This page states the predicate; shipping the alert rule itself is out of scope (see "Deliberately excluded" below).

## Metric families

### Supervision and collection outcome

Owned by the process manager itself, never per-process — `process_name` here identifies the process manager, not a supervised child.

The two `collection` families measure the scrape endpoint's own work of gathering each process's snapshot, so they exist **only when the scrape transport is enabled**. `opencrq.pm.processes` is recorded regardless and reaches both transports. This is the one place where the two transports do not carry the same set of families.

| Instrument name (push) | Exposed name (scrape) | Type | Unit | Attributes | Description |
|:--|:--|:--|:--|:--|:--|
| `opencrq.pm.processes` | `opencrq_pm_processes` | Gauge | `{process}` | `state`: one of `FORKING`, `BOOTING`, `STARTING`, `RUNNING`, `STOPPING`, `STOPPED`, `EXITED`, `ERROR` | The number of supervised processes currently in each lifecycle state. |
| `opencrq.pm.collection.ok` | `opencrq_pm_collection_ok` | Gauge | — | `supervised_process`: the name of the process the snapshot was collected from, including `pm` for the process manager's own | Whether the most recent metrics collection from a supervised process succeeded (1) or failed (0). |
| `opencrq.pm.collection.failures` | `opencrq_pm_collection_failures_total` | Counter | `{failure}` | `supervised_process`, `reason`: one of `timeout`, `error`, `not_connected`, `not_running`, `oversized`, `invalid`, `empty`, `send_error` | The number of metrics-collection attempts from a supervised process that failed, by reason. |

A collection outcome is only visible from the *following* scrape — every outcome is recorded after the snapshot the current response serves, so a reader sees one round's lag on this family, and the two collection families are absent altogether from the very first scrape after a start.

More generally, a family that is only recorded when something happens does not exist on the endpoint until it first happens. `opencrq_pm_collection_failures_total`, `db_client_connection_timeouts_total` and `opencrq_db_client_connection_reconnection_attempts_total` only ever appear once the corresponding failure has occurred, so a healthy deployment serves none of the three; `db_client_connection_wait_time` and `db_client_connection_use_time` appear on the first pool acquisition. Write panels and alerts on these so that an absent series is treated as zero. Note that `or vector(0)` is a blunt instrument for that: it yields a label-free series, so it breaks `by (...)` grouping, and it only fires when the selector is entirely empty — it fills nothing when a family exists for one `process_name` and not another.

### Node runtime (per process)

Contributed by two auto-instrumentations, `@opentelemetry/instrumentation-runtime-node` and `@opentelemetry/instrumentation-host-metrics`, with no code authored in this codebase — the descriptions and units below are theirs, reproduced verbatim as the endpoint serves them.

| Instrument name (push) | Exposed name (scrape) | Type | Unit | Attributes | Description |
|:--|:--|:--|:--|:--|:--|
| `nodejs.eventloop.utilization` | `nodejs_eventloop_utilization` | Gauge | `1` | — | Event loop utilization |
| `nodejs.eventloop.time` | `nodejs_eventloop_time_total` | Counter | `s` | `nodejs_eventloop_state`: `active` or `idle` | Cumulative duration of time the event loop has been in each state. |
| `nodejs.eventloop.delay.max` | `nodejs_eventloop_delay_max` | Gauge | `s` | — | Event loop maximum delay. |
| `nodejs.eventloop.delay.mean` | `nodejs_eventloop_delay_mean` | Gauge | `s` | — | Event loop mean delay. |
| `nodejs.eventloop.delay.p99` | `nodejs_eventloop_delay_p99` | Gauge | `s` | — | Event loop 99 percentile delay. |
| `v8js.gc.duration` | `v8js_gc_duration` | Histogram | `s` | — | Garbage collection duration, summed across every kind. |
| `v8js.memory.heap.used` | `v8js_memory_heap_used` | Gauge | `By` | `v8js_heap_space_name`: one V8 heap space per series (see the heap trap below) | Heap Memory size allocated. |
| `process.cpu.time` | `process_cpu_time_total` | Counter | `s` | `cpu_mode`: `user` or `system` | Process Cpu time in seconds |
| `process.cpu.utilization` | `process_cpu_utilization` | Gauge | — | `cpu_mode`: `user` or `system` | Process Cpu usage time 0-1 |
| `process.memory.usage` | `process_memory_usage` | Gauge | — | — | Process Memory usage in bytes |

`v8js.memory.heap.limit` is declared by `@opentelemetry/instrumentation-runtime-node` but wired to no collector at the pinned version — confirm it stays absent from the document on any version bump rather than assuming this note still holds; the set-equality test fails loudly if it starts appearing.

`v8js_gc_duration` is recorded per kind (`major`, `minor`, `incremental`, `weakcb`) by `instrumentation-runtime-node`, but no dashboard we operate reads that split, so a View both merges every kind into one series and overrides the served description to match — the row above is the text this codebase authors, not upstream's.

Eight further families this instrumentation declares are dropped by a View before they reach either transport, none of them read by a dashboard we operate: four event-loop-delay gauges, `nodejs.eventloop.delay.min`, `nodejs.eventloop.delay.stddev`, `nodejs.eventloop.delay.p50` and `nodejs.eventloop.delay.p90` (`max`, `mean` and `p99` are the three a human reads); three heap-space families, `v8js.memory.heap.space.size`, `v8js.memory.heap.space.available_size` and `v8js.memory.heap.space.physical_size` (each carrying about a dozen series, one per V8 heap space — see the heap trap below — and reporting the same heap spaces `v8js.memory.heap.used` already covers); and `v8js.resource.active` (the family behind the one unbounded-cardinality attribute this endpoint would otherwise carry, `v8js_resource_type` — see the cardinality note below).

**The heap trap.** V8 splits the heap into spaces, and `v8js_memory_heap_used` reports each space separately rather than one series per process. A panel asking "how much heap does a process use" must sum the spaces back together. `v8js_heap_space_name` takes one value per space the runtime reports — on Node 24, the version the shipped image runs, that is thirteen: `read_only_space`, `new_space`, `old_space`, `code_space`, `shared_space`, `trusted_space`, `shared_trusted_space`, `new_large_object_space`, `large_object_space`, `code_large_object_space`, `shared_large_object_space`, `shared_trusted_large_object_space` and `trusted_large_object_space`. The list is a property of the V8 version, not of this platform, so treat the count as "about a dozen per process" when budgeting series rather than as a fixed number. See the recording rules below for the worked aggregation.

**Cardinality.** No attribute on this page has an unbounded value set. `v8js_resource_type`, the one exception `@opentelemetry/instrumentation-runtime-node` would otherwise contribute — whatever libuv resource kinds the process currently holds (`Timeout`, `Immediate`, `TTYWrap`, `PipeWrap`, and so on), growing with what the application does — is dropped before it reaches either transport (see above).

### Connection pool (per process)

Authored in `back/src/engines/db/db-metrics.ts`, adopting the OpenTelemetry semantic-convention names for a database connection pool.

| Instrument name (push) | Exposed name (scrape) | Type | Unit | Attributes | Description |
|:--|:--|:--|:--|:--|:--|
| `db.client.connection.count` | `db_client_connection_count` | Gauge | `{connection}` | `db_client_connection_pool_name`, `db_client_connection_state`: `used` or `idle` | The number of connections currently in the state described by the state attribute. |
| `db.client.connection.pending_requests` | `db_client_connection_pending_requests` | Gauge | `{request}` | `db_client_connection_pool_name` | The number of callers currently waiting for a connection from the pool. |
| `db.client.connection.limit` | `db_client_connection_limit` | Gauge | `{connection}` | `db_client_connection_pool_name` | The maximum number of connections the pool is configured to open. |
| `db.client.connection.wait_time` | `db_client_connection_wait_time` | Histogram | `s` | `db_client_connection_pool_name` | The time it took to obtain an open connection from the pool, including waits that ended in a failure. |
| `db.client.connection.use_time` | `db_client_connection_use_time` | Histogram | `s` | `db_client_connection_pool_name` | The time between borrowing a connection and returning it to the pool. |
| `db.client.connection.timeouts` | `db_client_connection_timeouts_total` | Counter | `{timeout}` | `db_client_connection_pool_name` | The number of connection requests that timed out before a connection could be obtained. |

The pool name is `{server.address}:{server.port}/{db.namespace}` (e.g. `postgres:5432/opencrq`), matching the algorithm `instrumentation-pg` itself uses, so this pool identifier lines up with the one on the query-duration histogram below.

### Query duration (per process)

Adopted from `@opentelemetry/instrumentation-pg`'s own `db.client.operation.duration`, with its attributes bounded by a View rather than passed through raw.

| Instrument name (push) | Exposed name (scrape) | Type | Unit | Attributes | Description |
|:--|:--|:--|:--|:--|:--|
| `db.client.operation.duration` | `db_client_operation_duration` | Histogram | `s` | `server_address`, `server_port`, `db_namespace` (deployment-scoped: the configured Postgres host, port and database), `db_system_name` (always `postgresql`), `db_operation_name`, `error_type` | Duration of database client operations. |

The description above is `instrumentation-pg`'s own, served as written — this codebase adopts the instrument rather than declaring it, and the View that bounds its attributes does not change its description or its name.

`db_operation_name` is bounded to the DML kinds this codebase issues — `SELECT`, `INSERT`, `UPDATE`, `DELETE` — or the sentinel `_OTHER` for anything outside that set, transaction control (`BEGIN`, `COMMIT`, `ROLLBACK`) and DDL included: the read/write split those four give is what a panel reads, and `db_client_connection_use_time` already covers transaction-control latency. `error_type` carries a full five-character Postgres `SQLSTATE` code whose class is one of `08`, `22`, `23`, `25`, `28`, `3D`, `40`, `42`, `53`, `55`, `57`, `58` or `XX` (connection loss, data and constraint violations, invalid transaction state, authorization, missing catalog, serialization failure, syntax and access-rule errors, insufficient resources, operator intervention, system error, internal error), or its own sentinel `_OTHER` for a code outside those classes or a malformed one. The two sentinels render identically as the string `_OTHER` but bound two different attributes; do not conflate them.

A query that produced no error carries no `error_type` attribute at all — absence means "no error", never "unknown error". This attribute is also frequently absent **even on failures**: `instrumentation-pg` only sets it on one of its two internal recording paths, and this codebase's ORM predominantly takes the other. Do not build an alert on an error-rate breakdown from this histogram; it bounds the values that do arrive, it does not guarantee failures are distinguishable from successes.

### Reconnection (per process)

| Instrument name (push) | Exposed name (scrape) | Type | Unit | Attributes | Description |
|:--|:--|:--|:--|:--|:--|
| `opencrq.db.client.connection.reconnection_attempts` | `opencrq_db_client_connection_reconnection_attempts_total` | Counter | `{attempt}` | `db_client_connection_pool_name` | The number of attempts made to re-establish the database connection after the pool reported an error. |

### Change-driven recompute (job-running processes)

Authored in `back/src/utils/recompute-metrics.ts`. The dispatcher asks for a recompute when a table a job reads changes, when the change feed reconnects, as the follow-up of a change that landed while the job was running, or when the scheduler enqueues a job's daily occurrence.

| Instrument name (push) | Exposed name (scrape) | Type | Unit | Attributes | Description |
|:--|:--|:--|:--|:--|:--|
| `opencrq.recompute.asks` | `opencrq_recompute_asks_total` | Counter | `{ask}` | `path`: `dispatcher` or `manual`, `job_kind`, `already_existed`: `true` when the ask folded into a run already queued or running, `reason` (dispatcher path only): `change`, `reconnect`, `daily` or `followup` | Recompute enqueue requests, by path and dedup outcome. |
| `opencrq.recompute.unattributed_reads` | `opencrq_recompute_unattributed_reads_total` | Counter | `{read}` | `job_kind` | Reads an observed run could not attribute to a table, each of which widened the job's subscription to every table until its next clean run. |
| `opencrq.recompute.feeding_cycles` | `opencrq_recompute_feeding_cycles_total` | Counter | `{cycle}` | `job_kind`, `cycle`: the job kinds of the feeding cycle joined by ` -> ` | Job kinds whose observed reads and writes feed each other, counted once per distinct cycle per process rather than once per detection: every observation refresh of a job in a cycle detects it again. Reported only: every kind of the cycle keeps its change routing, the watermark each job scopes itself with being what makes the loop converge. The line beside it is a warning, not an error. |

`job_kind` is bounded by the registered job handlers, `reason` and `path` by the enums above, and `cycle` by the combinations of registered kinds, so none of these attributes grows with traffic. A non-zero `unattributed_reads` or `feeding_cycles` is an engineering signal, not an operational one: the job keeps running and keeps being routed, it is only asked more often than it needs until the code is fixed. A cycle that does not converge is reported separately, as an error log naming the kind and the job origins that keep asking for it.

### OpenCTI mirror rebuild (job-running processes)

Authored in `back/src/features/opencti-mirror-rebuild/mirror-rebuild-metrics.ts`. The mirror rebuild folds the marked rows of the OpenCTI stream mirror onto the actors they describe, one actor at a time. An actor whose rebuild raises leaves its rows marked and the run carries on with the rest, so the run still ends `succeeded`: this counter is the only place that loss is visible as a number.

| Instrument name (push) | Exposed name (scrape) | Type | Unit | Attributes | Description |
|:--|:--|:--|:--|:--|:--|
| `opencrq.opencti.mirror_rebuild.actors` | `opencrq_opencti_mirror_rebuild_actors_total` | Counter | `{actor}` | `outcome`: `rebuilt` (the actor was folded from what the mirror holds), `retracted` (the stream deleted its upstream object, so the actor is gone), `failed` (the rebuild raised and the actor's rows stay marked) | Actors one mirror rebuild settled, by outcome. |

`outcome` is a three-value enum, so the attribute does not grow with traffic. A `failed` count that keeps rising run after run is the operational signal: the rows of that actor stay marked, every later run picks them up again and fails the same way, so the backlog never drains. Alert on `increase(opencrq_opencti_mirror_rebuild_actors_total{outcome="failed"}[1h]) > 0` sustained across several windows, and read the `OpenCTI actor rebuild failed, its rows stay marked` log lines to get the STIX id. A single burst of `failed` alongside a healthy `rebuilt` count is a transient the next run absorbs.

### Doorbell resolution (http process)

Authored in `back/src/features/events/doorbell-metrics.ts`. Every tRPC query procedure is observed: a middleware scopes the call, the Drizzle logger of the shared database instance attributes each statement to it, and one `EXPLAIN (GENERIC_PLAN)` per distinct statement shape names the tables it reads (PostgreSQL 16 or later; on an older server every EXPLAIN is refused and every procedure widens). A `data_changed` doorbell is then resolved to the procedures that read a written table (a DELETE counting as a write of every table whose doorbell finds its tenant through the deleted row), plus every procedure whose reads the index cannot vouch for. The resolution is traced as `Doorbell resolved`, measured here, and sent to the client, which invalidates those procedures alone: a doorbell `resolution` of `all` (a LISTEN reconnect) is the only one that still invalidates its whole cache.

| Instrument name (push) | Exposed name (scrape) | Type | Unit | Attributes | Description |
|:--|:--|:--|:--|:--|:--|
| `opencrq.events.doorbells` | `opencrq_events_doorbells_total` | Counter | `{doorbell}` | `resolution`: `named` when the doorbell names procedures, `all` when it stands for every table (a LISTEN reconnect) | Doorbells resolved against the query dependency index. |
| `opencrq.events.doorbell.procedures` | `opencrq_events_doorbell_procedures` | Histogram | `{procedure}` | none | Number of query procedures one named doorbell makes stale. |
| `opencrq.events.query_index.explains` | `opencrq_events_query_index_explains_total` | Counter | `{explain}` | `outcome`: `ok` or `refused` | EXPLAIN statements run to learn the tables a query procedure reads. One per distinct statement shape, a bound list counting as one shape whatever its length, so the `ok` count plateaus once every code path has run. A refused EXPLAIN is retried on the statement's next occurrence. |
| `opencrq.events.query_index.procedures` | `opencrq_events_query_index_procedures` | Gauge | `{procedure}` | `state`: `observed` (a read set the doorbell can narrow on), `declared` (a read set the procedure declares in its tRPC meta, for a result no statement of its own reads), `widened` (a statement EXPLAIN refused, a plan that did not come back within the attribution deadline, or a read of a table without a doorbell), `without_reads` (a call completed and attributed no read) | Query procedures the index knows, by state. |

Only an `observed` or `declared` procedure is narrowed; the other two states are named on every doorbell, so each one costs every open tab a refetch on every change. `without_reads` is not "depends on nothing": a procedure returning what the tRPC context resolved (the session's own user) issues no statement of its own and still goes stale, so it is named, unless it declares the tables its result depends on (`.meta({ reads: [schema.users] })`, `[]` for a result held in process memory). A procedure that shows up there is one to declare. The log names a procedure as it widens (`Query procedure widened to every doorbell`, with the refused statement or the silent table); a refused count that keeps growing after the `ok` count has plateaued means a statement Postgres refuses to plan generically, and one that grows in bursts under load means the EXPLAIN could not get a connection. A call waits at most 250 ms for the plan of a statement it issued: the EXPLAIN borrows from the same pool the request does, so waiting for it unbounded would turn a saturated pool into five-second responses. Past that deadline the response goes out and the procedure is `widened` until the plan lands, which is what a `widened` count rising and falling under load means.

### tRPC procedures (http process)

Authored in `back/src/utils/trpc-telemetry.ts`, recorded by a middleware at the root of every tRPC procedure. Subscriptions are excluded on purpose: their resolution spans the stream's whole lifetime, which would poison a request-latency histogram.

| Instrument name (push) | Exposed name (scrape) | Type | Unit | Attributes | Description |
|:--|:--|:--|:--|:--|:--|
| `opencrq.trpc.server.duration` | `opencrq_trpc_server_duration` | Histogram | `s` | `trpc_type`: `query` or `mutation`, `error_type` (a tRPC error code such as `UNAUTHORIZED`, or `_OTHER` for anything outside that set; absent on success) | Duration of a single tRPC procedure call, output validation included, serialization excluded, aggregated across every procedure. |
| `opencrq.trpc.server.calls` | `opencrq_trpc_server_calls_total` | Counter | `{call}` | `trpc_procedure` (the dotted procedure path, e.g. `findingScore.getScoredFindings`), `trpc_type`, `error_type` | tRPC procedure calls, by procedure and outcome. |

The per-procedure breakdown lives on `opencrq.trpc.server.calls` alone: the duration histogram carries no `trpc_procedure` label, on purpose. The app router registers on the order of 250 procedures, and a per-procedure histogram would multiply that by every bucket — 250 procedures fully exercised against a 6-boundary histogram would expose over 2,000 lines for a breakdown no dashboard we operate reads (its latency panel already aggregates the procedure label away with `sum by (le)`). What is lost is more than per-procedure latency: `opencrq.trpc.server.response_items` is gone outright, so the response-size distribution disappears for every call, not just its per-procedure breakdown — the slow-call log below is the only place a size still survives, and only above its threshold. The `Slow tRPC call` log line is the substitute for diagnosing which call got slow, at the cost of no baseline for calls under its threshold. `trpc_procedure` is bounded by the app router's procedure set, and `error_type` by tRPC's own error-code enum, which the middleware checks the value against rather than trusting it: an abnormal throw carries whatever `code` its own library set, a Postgres SQLSTATE or a Node errno included. Both are closed sets that only grow with a code change — but the worst case is already past the SDK's default 2000-series-per-instrument `aggregationCardinalityLimit`: 250 procedures times tRPC's twenty-odd error codes (plus absent, for success) is comfortably into the thousands. In practice a given procedure only ever produces a handful of its possible codes, which is what keeps this instrument under the limit today; past it, the SDK folds the overflow into `otel.metric.overflow` rather than growing the family further, so a newly-noisy procedure would degrade silently rather than error.

A call at or above 500ms is also written to the log as `Slow tRPC call`, carrying its procedure, duration and item count: the histogram says calls got slower in aggregate, that line says which call did and how much data it was carrying.

## Scrape recipe

One target per container, structurally identical to an OpenCTI scrape block — only the job name differs. The port does not: `14269` is OpenCTI's own default for the same endpoint, and OpenCRQ adopted it deliberately so the two blocks stay interchangeable. The process breakdown arrives as the `process_name` label on every series, not as separate targets: an operator never needs to know how many processes a deployment's profile runs.

```yaml
scrape_configs:
  - job_name: opencrq
    # Must stay above TELEMETRY__OBSERVABILITY__PROMETHEUS__MIN_INTERVAL_MS (default 5000):
    # inside that window the endpoint re-serves its cached document.
    scrape_interval: 15s
    static_configs:
      - targets: ["opencrq:14269"] # host:TELEMETRY__OBSERVABILITY__PROMETHEUS__PORT
```

Building a panel: aggregate across processes as [Composition across processes](#composition-across-processes) describes — `sum` for an additive family, the statistic's own operator for the event-loop delay gauges and the utilisation ratios — so the panel is correct whether the container runs one process or several. Group by `instance` to keep containers apart. For example, total in-use database connections per container:

```promql
sum by (instance) (db_client_connection_count{db_client_connection_state="used"})
```

No authentication or TLS is included above — the endpoint is unauthenticated by design and protected by network placement (see above), so a recipe adding either would contradict how it is meant to be reached.

## Recording rules: making existing OpenCTI runtime dashboards resolve

These rules translate OpenCRQ's Node-runtime names into the names an [`opentelemetry-node-metrics`](https://github.com/marcbachmann/opentelemetry-node-metrics) (v3.0.0, the package OpenCTI's own runtime dashboards are built against) exporter would have produced, so an existing OpenCTI runtime panel resolves against an OpenCRQ target without being rewritten — as long as the panel carries no `job` selector of its own: the recorded series keep `job="opencrq"` (see the rules below), so a panel pinned to OpenCTI's own job name still needs that selector dropped or relabeled. They apply **only** to the Node-runtime family group above — OpenCTI has no pool, query or supervision dashboards, so no rule is needed for those. Every rule is marked as a **rename** (same quantity, different name), an **aggregation** (OpenCRQ splits what the other side reports whole), or **no equivalent** (the two measure different things and no expression converts one into the other); a family OpenCRQ no longer exposes carries no row here — see [Node runtime](#node-runtime-per-process) for the eight the cardinality cuts dropped. Emit no alias series from the product itself: the mapping lives here, as a recording rule the operator applies on top of their own scrape — never as a second name OpenCRQ serves.

| OpenCRQ name | OpenCTI-compatible name | Category |
|:--|:--|:--|
| `nodejs_eventloop_delay_max` | `nodejs_eventloop_lag_max_seconds` | Rename |
| `nodejs_eventloop_delay_mean` | `nodejs_eventloop_lag_mean_seconds` | Rename |
| `nodejs_eventloop_delay_p99` | `nodejs_eventloop_lag_p99_seconds` | Rename (worst process — see below) |
| `v8js_gc_duration` | `nodejs_gc_duration_seconds` | Rename — OpenCRQ's series carries no `kind` label (a View merges the four GC kinds into one series), so a panel on the OpenCTI side that groups by `kind` sees one unlabelled series instead of four; the bucket boundaries are not the same either (see below) |
| `v8js_memory_heap_used` (label `v8js_heap_space_name`) | `nodejs_heap_space_size_used_bytes` (label `space`) | Rename, including the label key and its values |
| `v8js_memory_heap_used` (summed across spaces) | `nodejs_heap_size_used_bytes` | Aggregation (approximate — see below) |
| `process_memory_usage` | `process_resident_memory_bytes` | Rename |
| `process_cpu_time_total{cpu_mode="user"}` | `process_cpu_user_seconds_total` | Rename (one label value selected) |
| `process_cpu_time_total{cpu_mode="system"}` | `process_cpu_system_seconds_total` | Rename (one label value selected) |
| `process_cpu_time_total` (summed across `cpu_mode`) | `process_cpu_seconds_total` | Aggregation |
| `nodejs_eventloop_utilization` | *(none)* | No equivalent — the other side reports event-loop health as a lag sample (`nodejs_eventloop_lag_seconds`, taken via `setImmediate`), a duration; this is a busy ratio. Neither derives from the other. **Instead:** point the panel at `nodejs_eventloop_lag_mean_seconds`, which the rules below do produce, or query `nodejs_eventloop_utilization` directly and relabel the panel's axis as a ratio. |
| `process_cpu_utilization` | *(none)* | No equivalent — the other side exposes only cumulative CPU-seconds counters, never a ratio. **Instead:** build the panel on `rate(process_cpu_seconds_total[5m])`, which the rules below produce and which reads as cores consumed — the quantity such a panel is usually after. |
| `nodejs_eventloop_time_total` | *(none)* | No equivalent — no cumulative time-in-state counter exists on the other side. **Instead:** there is no OpenCTI panel to preserve; query the OpenCRQ name directly if you want the breakdown. |

**Aggregation across processes.** These rules aggregate exactly as [Composition across processes](#composition-across-processes) describes, with one addition: for `p99` no cross-process recombination exists at all, so that rule takes the **worst process** (`max`) — a defensible alerting signal, but *not* the container's percentile. Label that panel accordingly. The `nodejs_heap_size_used_bytes` aggregation is likewise approximate: OpenCTI's series is `process.memoryUsage().heapUsed`, whereas summing V8 heap spaces measures a closely related but not identical quantity.

**The GC histogram's buckets do not line up.** The name renames cleanly, but OpenCRQ's `v8js.gc.duration` ships the boundaries `0.001, 0.01, 0.1, 1, 5`, which are not the ones the other side's histogram carries. A `histogram_quantile` panel still resolves and still gives a sensible answer; a panel or alert that selects a specific `le` value does not. Re-bucket at the source if you depend on one.

**Before you install these:** the rules record over metric names that `prom-client`-based exporters also serve — `process_cpu_seconds_total`, `process_cpu_user_seconds_total`, `process_cpu_system_seconds_total` and `process_resident_memory_bytes` are standard names, and on the very Prometheus that also scrapes OpenCTI they will already exist. Every rule below therefore selects with `job="opencrq"`; change it if you named the job differently, and do not drop it, or the two products' series are folded together irreversibly at recording time. Save the group to a file and reference it from your Prometheus configuration:

```yaml
rule_files:
  - opencrq-opencti-compat.yml
```

```yaml
groups:
  - name: opencrq-opencti-compat
    rules:
      - record: nodejs_eventloop_lag_max_seconds
        expr: max without (process_name) (nodejs_eventloop_delay_max{job="opencrq"})
      # Exact only when every process observed the same sample count.
      - record: nodejs_eventloop_lag_mean_seconds
        expr: avg without (process_name) (nodejs_eventloop_delay_mean{job="opencrq"})
      # A percentile cannot be recombined across processes: this reports the worst process,
      # not the container.
      - record: nodejs_eventloop_lag_p99_seconds
        expr: max without (process_name) (nodejs_eventloop_delay_p99{job="opencrq"})
      - record: process_resident_memory_bytes
        expr: sum without (process_name) (process_memory_usage{job="opencrq"})
      # The heap trap: sum the V8 heap spaces back together to get one value per container.
      - record: nodejs_heap_size_used_bytes
        expr: sum without (process_name, v8js_heap_space_name) (v8js_memory_heap_used{job="opencrq"})
      # The per-space series the other side also exposes. Its label is `space`, and its values
      # drop the `_space` suffix that V8 reports, so both the key and the value are rewritten.
      - record: nodejs_heap_space_size_used_bytes
        expr: sum without (process_name, v8js_heap_space_name) (label_replace(v8js_memory_heap_used{job="opencrq"}, "space", "$1", "v8js_heap_space_name", "(.*)_space"))
      - record: process_cpu_user_seconds_total
        expr: sum without (process_name, cpu_mode) (process_cpu_time_total{job="opencrq", cpu_mode="user"})
      - record: process_cpu_system_seconds_total
        expr: sum without (process_name, cpu_mode) (process_cpu_time_total{job="opencrq", cpu_mode="system"})
      - record: process_cpu_seconds_total
        expr: sum without (process_name, cpu_mode) (process_cpu_time_total{job="opencrq"})
      # OpenCRQ's own series carries no `kind` label (a View merges the four GC kinds into one
      # series), so this only sums across processes — a panel grouping by `kind` sees one series.
      - record: nodejs_gc_duration_seconds_bucket
        expr: sum without (process_name) (v8js_gc_duration_bucket{job="opencrq"})
      - record: nodejs_gc_duration_seconds_sum
        expr: sum without (process_name) (v8js_gc_duration_sum{job="opencrq"})
      - record: nodejs_gc_duration_seconds_count
        expr: sum without (process_name) (v8js_gc_duration_count{job="opencrq"})
```

Built against OpenCTI 7.260701.0. The names above come from `opentelemetry-node-metrics`, which OpenCTI has pinned at `3.0.0` since 5.4.0, so this mapping applies unchanged to any OpenCTI from 5.4.0 onward. A rule pointing at a name a given OpenCTI deployment does not serve is a correctable documentation issue, not a breaking change to OpenCRQ's own contract.

The heap-space value rewrite assumes every V8 heap space name ends in `_space`, which holds on the Node versions this platform ships. If a future runtime reported one that did not, `label_replace` would leave that series untouched and the enclosing `sum without (..., v8js_heap_space_name)` would fold every such space into a single unlabelled series whose value is their total — wrong rather than merely unlabelled, so re-check this if the heap-space list changes.

## Deliberately excluded

Dashboards and alert definitions are not shipped here — this page states the one alert condition that matters (pool saturation, above) and gives worked panel expressions, but shipping ready-made panels or alert rules is out of scope.
