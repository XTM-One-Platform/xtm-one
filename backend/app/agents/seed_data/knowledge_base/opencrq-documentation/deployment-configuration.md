# ⚙️ Configuration & Environment Guide

This guide details the environment variables and configuration options for deploying OpenCRQ.

## Process Manager

| Variable                    | Required | Default Value | Architectural/Functional Impact                                                   |
|:----------------------------|:--------:|:--------------|:----------------------------------------------------------------------------------|
| `PROCESS_MANAGER__PROFILE`  |   Yes    | —             | `split` runs the HTTP server and the background worker as separate processes, each with its own database connection pool. `monolith` runs everything in a single process and is only intended for local development — never use it on an environment with real data volume. |

## Database (PostgreSQL)

**Version.** PostgreSQL 17 is the reference version: the one the XTM bundle runs OpenCRQ beside XTM One on (`pgvector/pgvector`, PostgreSQL 17 with pgvector), and the one the development stack and CI run. OpenCRQ uses no pgvector itself.

| Variable             | Required | Default Value | Architectural/Functional Impact                                       |
|:---------------------|:--------:|:--------------|:----------------------------------------------------------------------|
| `POSTGRES__HOST`     |   Yes    | `postgres`    | Hostname or IP of the PostgreSQL service. Used by the App to connect. |
| `POSTGRES__PORT`     |   Yes    | `5432`        | Listening port of the PostgreSQL service.                             |
| `POSTGRES__USER`     |   Yes    | `opencrq`     | User for DB authentication and Postgres container initialization.     |
| `POSTGRES__PASSWORD` |   Yes    | `changeme`    | Password for DB authentication.                                       |
| `POSTGRES__DATABASE` |   Yes    | `opencrq`     | Name of the target database.                                          |
| `POSTGRES__POOL_MAX` |    No    | `20`          | Maximum number of PostgreSQL connections in the shared pool of one application process. Increase on high-volume environments. What must stay under PostgreSQL's own `max_connections` is the sum over every process, and a process opens connections this pool does not count: a control pool of 5 on every process that runs jobs, reserved for the job runtime's own claims, lease renewals and log writes so they never queue behind a handler's reads, and one long-lived client per `LISTEN` channel the process subscribes to. |

**Shared memory.** A parallel query passes tuples between its workers through `/dev/shm`, and an aggregate over a large asset estate exceeds the 64 MB a container gets by default: the query then fails outright with `could not resize shared memory segment`. Give the PostgreSQL service at least 1 GB (`shm_size: 1gb` in Compose, an `emptyDir` of `medium: Memory` sized to match on Kubernetes); the development stack and the XTM Docker bundle already do.

**Extensions.** The migrations create the `unaccent` extension. It is trusted, so the database owner can create it without superuser rights, but a managed service that allowlists extensions one by one refuses any it does not list, and the migration then fails at startup. On Azure Database for PostgreSQL Flexible Server, add it to the `azure.extensions` server parameter before the first start.

## File Storage (RustFS / S3-compatible)

OpenCRQ stores uploaded files (e.g. imported bundles) in an S3-compatible object store. The development stack ships [RustFS](https://github.com/rustfs/rustfs) and the XTM Docker bundle Silo, but any S3-compatible backend (MinIO, AWS S3, …) works. Files are scoped per tenant under the key `{tenantId}/{storageKey}`, where `storageKey` is a UUIDv7 generated per upload. The store is only contacted by the backend — there is no direct browser access.

File storage is **optional configuration**. Boot behavior depends on the `FILE_STORAGE__*` variables below:
- **Unset entirely** — the app boots normally with the feature disabled: no upload/download routes are mounted, and the frontend hides the corresponding actions.
- **Set but incomplete or wrong** (missing/bad credentials, bad bucket) — the app fails fast at boot with a clear error, since this is almost certainly a deployment mistake.
- **Set and valid, but the endpoint is temporarily unreachable** — the app boots normally, logs a non-fatal error, and retries lazily on the first real upload/download attempt (no blocking wait, no crash).

In **non-production**, the bucket is created automatically at startup if missing. In **production**, the bucket must be created ahead of time (see the ops note below) — the app fails fast with a helpful message if the bucket does not exist.

| Variable                    | Required (when enabling) | Default Value | Architectural/Functional Impact                                                                                    |
|:----------------------------|:------------------------:|:--------------|:-------------------------------------------------------------------------------------------------------------------|
| `FILE_STORAGE__ENDPOINT`   | **Yes**                  | -             | S3 API endpoint of the object store (e.g. `http://minio:9000` inside the XTM Docker bundle). Enables the feature when set together with the access/secret key.  |
| `FILE_STORAGE__ACCESS_KEY` | **Yes**                  | -             | Access key for the object store. Pass through a secret store (Vault, K8s Secret, SOPS, CI secret).                 |
| `FILE_STORAGE__SECRET_KEY` | **Yes**                  | -             | Secret key for the object store. Pass through a secret store — never a `ConfigMap` or committed JSON file.         |
| `FILE_STORAGE__BUCKET`     |    No    | `opencrq`     | Bucket that holds all tenant files.                                                                                |
| `FILE_STORAGE__REGION`     |    No    | `eu-west-3`   | S3 region. Arbitrary for RustFS/MinIO but required by the S3 client.                                               |

> **Production ops — create the bucket first.** Auto-creation is disabled in production to avoid masking a misconfigured endpoint or credentials. Create the bucket once before first boot, e.g. with the AWS CLI against your endpoint:
> ```bash
> aws --endpoint-url "$FILE_STORAGE__ENDPOINT" s3 mb "s3://$FILE_STORAGE__BUCKET"
> ```
> or through your provider's console. The RustFS web console is available on port `9001` in the local dev stack.

## Application (Backend/Frontend)

| Variable                               | Required | Default Value                      | Architectural/Functional Impact                                                            |
|:---------------------------------------|:--------:|:-----------------------------------|:-------------------------------------------------------------------------------------------|
| `HTTP_SERVER__PUBLIC_PORT`             |    No    | -                                  | Port the host publishes the application on, logged beside the internal one.                |
| `HEALTHCHECK_HTTP_SERVER__PUBLIC_PORT` |    No    | -                                  | Port the host publishes the healthcheck on, logged beside the internal one.                |
| `HTTP_SERVER__HOST`                    | **Yes**  | -                                  | Internal listening interface, `0.0.0.0` in a container.                                    |
| `HTTP_SERVER__PORT`                    | **Yes**  | -                                  | Internal listening port of the application, `3002`, which the published image exposes.     |
| `HEALTHCHECK_HTTP_SERVER__PORT`        | **Yes**  | -                                  | Internal listening port for healthchecks, `8444`, which the published image exposes.       |
| `APP__ADMIN__TOKEN`                    |    No    | -                                  | Bootstrap admin token, a UUIDv4 (the counterpart of OpenCTI's `APP__ADMIN__TOKEN`). It authenticates the peer provisioning API below and is an admin bearer on the API (see [Admin token as a bearer](#admin-token-as-a-bearer)); unset, both are off. Pass through a secret store. |
| `XTM__OPENCTI_URL`                     |    No    | -                                  | URL of the deployment's OpenCTI, where the product switcher sends a person. Unset: the URL of the tenant's OpenCTI connection, if any. |
| `XTM__OPENAEV_URL`                     |    No    | -                                  | URL of the deployment's OpenAEV, for the product switcher. Unset: the URL of the tenant's OpenAEV connection, if any. |

### Peer provisioning API

A deployment manager gives a sibling XTM product (an OpenAEV or an OpenCTI reading this platform) a service account and a token here, with the bootstrap admin token as bearer. No session and no tenant header: the account is created in the initial tenant (`INITIAL_TENANT__ID`), in its `connectors` group.

- `POST /api/xtm/peer-tokens` with `Authorization: Bearer <APP__ADMIN__TOKEN>` and `{"consumer": "openaev", "name": "OpenAEV", "email": "openaev@example.com", "token_name": "opencrq-link"}`. Idempotent on `email` (lower-cased): the same account is converged and renamed, its live tokens named `token_name` are revoked, and a non-expiring `flgrn_ocrq_tkn_` token is issued. Answers `200 {"user_id", "email", "name", "token_name", "token"}`; the token is shown once.
- `POST /api/xtm/peer-tokens/revoke` with the same bearer and `{"email", "token_name"}`: revokes those tokens. Answers `200 {"revoked": <count>}`.

A missing or wrong bearer answers 401, an invalid body 400.

### Admin token as a bearer

As with OpenCTI's `APP__ADMIN__TOKEN`, a sibling product of a single-token deployment (xtm-docker) calls OpenCRQ's API with `Authorization: Bearer <APP__ADMIN__TOKEN>`, with no init step issuing a `flgrn_ocrq_tkn_` token first. At every start OpenCRQ converges a service account named "Deployment admin token" in the initial tenant (`INITIAL_TENANT__ID`), in its `admin` group (every capability, every marking that group is cleared for), and a request carrying the admin token acts as that account. No token is stored: the bearer is compared in constant time with the configured value. Unset, or while the initial tenant does not exist, the admin token is no bearer. Rotating it is a restart with the new value. A SaaS deployment, whose peers get their own `flgrn_ocrq_tkn_` tokens from the provisioning API, keeps the admin token in its secret store and never hands it to a peer.

## Telemetry

Every `TELEMETRY__*` variable sits under one of two groups: `TELEMETRY__USAGE__*`, the [anonymous usage telemetry](usage-telemetry.md) sent to Filigran, and `TELEMETRY__OBSERVABILITY__*`, the monitoring data sent to your own tools. A `TELEMETRY__*` variable the platform does not know, a superseded one included, fails the startup rather than being ignored. The error names the configuration key under its path, e.g. `Unrecognized key: "traces"` under `telemetry` for `TELEMETRY__TRACES__ENABLED`.

The *OpenCTI equivalent* column maps each variable to its OpenCTI counterpart, for operators who already run OpenCTI. The two differ by design in three places:

- OpenCTI keeps its two groups under separate roots, `APP__TELEMETRY__*` for observability and `TELEMETRY_MANAGER__*` for usage telemetry; OpenCRQ keeps both under `TELEMETRY__*`.
- OpenCTI turns metrics on with one switch, `APP__TELEMETRY__METRICS__ENABLED`, that covers both transports; OpenCRQ turns each transport on with its own `ENABLED`.
- OpenCTI has no OTLP log export, and its Prometheus endpoint has none of the collection bounds below, since OpenCRQ's endpoint merges every process of the container.

### Usage telemetry

| Variable                    | Required | Default Value | Architectural/Functional Impact | OpenCTI equivalent |
|:----------------------------|:--------:|:--------------|:--------------------------------|:-------------------|
| `TELEMETRY__USAGE__ENABLED` |    No    | `true`        | Turns the [anonymous usage telemetry](usage-telemetry.md) sent to Filigran on or off. `false`: the platform never contacts the Filigran collector. | `TELEMETRY_MANAGER__ENABLED` |
| `TELEMETRY__USAGE__TAGS`    |    No    | (empty)       | Comma-separated deployment tags (e.g. `saas,eu-west`) attached to the usage telemetry as the `filigran.telemetry.tags` resource attribute. Normalized (trimmed, lowercased, deduplicated, sorted) before export. Empty by default: no tags attribute is exported. | `TELEMETRY_MANAGER__TAGS` |

### Observability

See [Metrics](metrics.md) for the full contract of every family the Prometheus and OTLP metrics transports expose, a scrape recipe and OpenCTI-compatible recording rules.

| Variable                                 | Required | Default Value      | Architectural/Functional Impact | OpenCTI equivalent |
|:-----------------------------------------|:--------:|:-------------------|:--------------------------------|:-------------------|
| `TELEMETRY__OBSERVABILITY__SERVICE_NAME` |    No    | `opencrq-platform` | Service name of every OTLP signal and of the Prometheus endpoint (`service.name`), and Pyroscope application name. | None for `service.name`, fixed to `opencti-platform`; `APP__TELEMETRY__PYROSCOPE__IDENTIFIER` for Pyroscope |

#### OpenTelemetry (Traces, Metrics, Logs)

| Variable                                             | Required | Default Value | Architectural/Functional Impact | OpenCTI equivalent |
|:-----------------------------------------------------|:--------:|:--------------|:--------------------------------|:-------------------|
| `TELEMETRY__OBSERVABILITY__OTLP__TRACES__ENABLED`    |    No    | `false`       | Enable or disable OpenTelemetry traces. | `APP__TELEMETRY__TRACING__ENABLED` |
| `TELEMETRY__OBSERVABILITY__OTLP__TRACES__EXPORTER`   |    No    | -             | OTLP endpoint for exporting traces (e.g., `http://otel-collector:4317`). **Required** when traces are enabled. | `APP__TELEMETRY__TRACING__EXPORTER_OTLP` |
| `TELEMETRY__OBSERVABILITY__OTLP__METRICS__ENABLED`   |    No    | `false`       | Enable or disable the OTLP metrics push transport. | `APP__TELEMETRY__METRICS__ENABLED`, which covers both metrics transports |
| `TELEMETRY__OBSERVABILITY__OTLP__METRICS__EXPORTER`  |    No    | -             | OTLP endpoint for exporting metrics (e.g., `http://otel-collector:4317`). **Required** when OTLP metrics are enabled. | `APP__TELEMETRY__METRICS__EXPORTER_OTLP` |
| `TELEMETRY__OBSERVABILITY__OTLP__LOGS__ENABLED`      |    No    | `false`       | Enable or disable OpenTelemetry logs. | None |
| `TELEMETRY__OBSERVABILITY__OTLP__LOGS__EXPORTER`     |    No    | -             | OTLP endpoint for exporting logs (e.g., `http://otel-collector:4317`). **Required** when logs are enabled. | None |

#### Prometheus

| Variable                                                     | Required | Default Value | Architectural/Functional Impact | OpenCTI equivalent |
|:-------------------------------------------------------------|:--------:|:--------------|:--------------------------------|:-------------------|
| `TELEMETRY__OBSERVABILITY__PROMETHEUS__ENABLED`              |    No    | `false`       | Enable or disable the Prometheus scrape endpoint. | `APP__TELEMETRY__METRICS__ENABLED`, which covers both metrics transports |
| `TELEMETRY__OBSERVABILITY__PROMETHEUS__PORT`                 |    No    | `14269`       | Port the Prometheus scrape endpoint listens on. | `APP__TELEMETRY__METRICS__EXPORTER_PROMETHEUS` |
| `TELEMETRY__OBSERVABILITY__PROMETHEUS__HOST`                 |    No    | `0.0.0.0`     | Bind address of the scrape endpoint. Binding it to loopback makes it unreachable from outside the container while the application keeps serving. | `APP__TELEMETRY__METRICS__EXPORTER_PROMETHEUS_HOST` |
| `TELEMETRY__OBSERVABILITY__PROMETHEUS__PUBLIC_PORT`          |    No    | -             | Port the scrape endpoint is advertised on, when it is published under a different one. | None |
| `TELEMETRY__OBSERVABILITY__PROMETHEUS__COLLECT_TIMEOUT_MS`   |    No    | `2500`        | Per-process bound on one collection. A process exceeding it is reported as a collection outcome rather than delaying the response. | None |
| `TELEMETRY__OBSERVABILITY__PROMETHEUS__ROUND_DEADLINE_MS`    |    No    | `4500`        | Bound on a whole collection round. Must be strictly above the per-process timeout, and is deliberately not a multiple of it so the two bounds stay distinguishable. | None |
| `TELEMETRY__OBSERVABILITY__PROMETHEUS__MIN_INTERVAL_MS`      |    No    | `5000`        | Minimum interval between two collections. Requests arriving inside it are served the cached document, failures included. | None |
| `TELEMETRY__OBSERVABILITY__PROMETHEUS__MAX_REPLY_BYTES`      |    No    | `2097152`     | Maximum size of one process's contribution. A larger one is refused and reported as a collection outcome. | None |

#### Pyroscope

| Variable                                              | Required | Default Value | Architectural/Functional Impact | OpenCTI equivalent |
|:------------------------------------------------------|:--------:|:--------------|:--------------------------------|:-------------------|
| `TELEMETRY__OBSERVABILITY__PYROSCOPE__ENABLED`        |    No    | `false`       | Enable or disable Pyroscope profiling. | `APP__TELEMETRY__PYROSCOPE__ENABLED` |
| `TELEMETRY__OBSERVABILITY__PYROSCOPE__SERVER_ADDRESS` |    No    | -             | URL used by the App agent to send profiles. **Required** when Pyroscope is enabled (e.g. `http://pyroscope:4040` in the development Compose file). | `APP__TELEMETRY__PYROSCOPE__EXPORTER` |

## Jobs (background runtime)

The job runtime claims and executes background work: connector syncs, score projections and the like. It runs in the `worker` process under the `split` profile, and in the single process under `monolith`. Every runtime process registers itself in a `workers` table on a heartbeat, and the integrations page reads that registry as one boolean to explain a queue that is not advancing. The registry is never exposed as rows, a count or a process identity: it says only whether anything is answering.

| Variable                          | Required | Default Value | Architectural/Functional Impact                                                                                                                                                                                          |
|:----------------------------------|:--------:|:--------------|:--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `JOBS__ENABLED`                   |    No    | `false`       | Master switch for the job runtime. The block is optional and unset reads as off, so it must be explicitly set to `true` to run. With it off, nothing claims queued jobs, and the integrations page says so: the no-worker banner is permanent, which is the honest reading.                                            |
| `JOBS__MAX_CONCURRENT`            |    No    | `4`           | Jobs a single runtime process executes at once.                                                                                                                                                                          |
| `JOBS__POLLING_FALLBACK_MS`       |    No    | `30000`       | How often the runtime polls for work when no `LISTEN/NOTIFY` wake-up arrives.                                                                                                                                             |
| `JOBS__HEARTBEAT_INTERVAL_MS`     |    No    | `10000`       | How often each runtime process refreshes its registry row. The beat is unconditional: it does not depend on the process holding work, so an idle worker is never mistaken for an absent one.                              |
| `JOBS__HEARTBEAT_STALE_AFTER_MS`  |    No    | `30000`       | How long a heartbeat stays credible. Past it, the platform reads as having no worker and the banner appears. It must exceed `JOBS__HEARTBEAT_INTERVAL_MS`: a value at or below the interval is refused when the config is parsed at boot. Keep it a comfortable multiple of the interval: barely above it, ordinary scheduling jitter raises the banner. |
| `JOBS__LOG_RETENTION_RUNS`        |    No    | `3`           | Runs of one job kind whose captured log lines are kept, per tenant. A run that ends deletes the lines of the older ones, and a connector's Logs tab renders exactly what this keeps. |
| `JOBS__LOG_RETENTION_LINES`       |    No    | `500`         | Captured lines kept for the OpenCTI stream session, which never ends and so is bounded by count rather than by run. |
| `JOBS__RUN_RETENTION_DAYS`        |    No    | `30`          | Days a finished run (`succeeded`, `cancelled`, `dlq`) is kept before the daily `jobs-retention` job purges its `jobs` row, taking its errors and captured log lines with it by cascade. The last finished run of each job kind is kept whatever its age, so a kind that runs rarely does not read as never ran. |

## Recompute (scheduled scoring)

Scores are recomputed whenever the data they read changes, and once a day whatever happened, so the inputs that move with the calendar alone are caught. The daily run is a row of the job schedule, read on the Schedules tab of Settings, Jobs next to the connectors' own, and enqueued once it is due: a platform down across its hour runs it once on return instead of skipping the day, and a day whose run failed is retried with a growing delay until the next occurrence supersedes it.

| Variable              | Required | Default Value | Architectural/Functional Impact                                                                                                                                                                                                                                                                                        |
|:----------------------|:--------:|:--------------|:-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `RECOMPUTE__DAILY_AT` |    No    | `03:00`       | `HH:MM` on a 24-hour UTC clock, the hour from which each tenant's full recompute is due. The day boundary it exists to catch is UTC too. Tenants are spread deterministically across the hour that follows it, so a single instant does not put every tenant at once against a `JOBS__MAX_CONCURRENT` shared by all job kinds. |
| `RECOMPUTE__DEBOUNCE_FACTOR` | No | `1` | Multiplier on the last run's duration in the throttle delay, so a recompute that costs a minute is not asked for every five seconds. |
| `RECOMPUTE__DEBOUNCE_FLOOR_MS` | No | `500` | Shortest delay between the first event of a burst and the run it books. It is also the fastest cadence a continuously written table can drive a job at: the events arriving inside the delay fold into the run already booked, they never postpone it. |
| `RECOMPUTE__DEBOUNCE_CAP_MS` | No | `300000` | Longest that delay can grow to, whatever the last run cost. |
| `RECOMPUTE__SCORE_HISTORY_RETENTION_DAYS` | No | `365` | Days of finding score history kept, trimmed at the end of each run. Twelve months, the longest window a finding's score history offers on its page. A finding always keeps the line in force when the window opens, however old it is, so one whose score has not moved in a year still counts in every past figure. |

## Platform Seed (non-production)

The platform seed populates the database with demo tenants, users, organizations and assets. It's intended for **non-production environments** — local dev workstations, CI sandboxes, ephemeral test platforms, demo instances — and should **never be enabled in production**, where the platform starts with its [initial tenant](#initial-tenant) and XTM One's administrator signs in first, as OpenCRQ's super administrator. On an empty database, the seed leaves only the demo tenants; a platform that booted once without it keeps its initial tenant beside them.

When enabled, the boot sequence creates a platform administrator (in a platform group holding every platform capability) and the demo users, none with an OpenCRQ password: everyone signs in through XTM One. If `PLATFORM_SEED__ENABLED=true` is set, both the admin email and password are required — boot fails fast otherwise (enforced by Zod at config parse time); the password is the one the seeded users get in XTM One, and OpenCRQ stores it nowhere. The seed is idempotent: it skips on subsequent boots once the platform has any user.

With `PLATFORM_SEED__XTM_ONE_ADMIN__*` set, every seeded user is first created in the XTM One of the `xtm_one` block, as a local account with the seeded admin's password, signed in as that XTM One administrator, and its OpenCRQ account is tied to the XTM One id, so the seeded users sign in through XTM One. A user XTM One already holds is reused rather than created again, and only a development build also gives it the seeded password and, at every boot, ties to that XTM One the seeded users of an already-seeded database; any other refusal fails the seed, and the platform boots without it. `yarn env:up` sets it on a development stack. Without it, the seed ties none of its users to XTM One, so none of them can sign in, and logs a warning saying so; the demo data is still there, for a service account token or for XTM One's own administrator, who reaches every tenant.

| Variable                                    |  Required  | Default Value | Architectural/Functional Impact                                                                                                |
|:--------------------------------------------|:----------:|:--------------|:-------------------------------------------------------------------------------------------------------------------------------|
| `PLATFORM_SEED__ENABLED`                    |     No     | `false`       | Master switch for the platform seed. Must stay `false` in production.                                                          |
| `PLATFORM_SEED__ADMIN__EMAIL`               | If enabled | -             | Email of the bootstrap platform admin user.                                                                                    |
| `PLATFORM_SEED__ADMIN__PASSWORD`            | If enabled | -             | The seeded users' XTM One password. Pass through a secret store (Vault, K8s Secret, SOPS, CI secret) — never a `ConfigMap` or committed JSON file. |
| `PLATFORM_SEED__XTM_ONE_ADMIN__EMAIL`       |     No     | -             | Email of an administrator of the XTM One in `XTM_ONE__URL`, which the seed signs in as to create its users there.             |
| `PLATFORM_SEED__XTM_ONE_ADMIN__PASSWORD`    | With the email | -           | That administrator's password, from a secret store like the admin's.                                                           |
| `PLATFORM_SEED__LARGE_ESTATE__ENABLED`      |     No     | `false`       | Adds a park of several hundred thousand assets, their findings and one closed risk scope mapping part of them, on top of the ordinary demo data. For performance work only: it writes over a million rows and takes minutes. See [the large estate](../development/development.md#the-large-estate). |
| `PLATFORM_SEED__LARGE_ESTATE__ASSETS`       |     No     | `100000`      | How many supporting assets the profile generates, in dependency chains three levels deep. Read only when the profile is enabled. |
| `PLATFORM_SEED__LARGE_ESTATE__FINDINGS`     |     No     | `300000`      | How many findings it hangs on them, over the CVEs the ordinary seed wrote. Read only when the profile is enabled.               |
| `PLATFORM_SEED__LARGE_ESTATE__MAPPED_ASSETS`|     No     | `30000`       | How many of those assets the closed risk scope is measured over. At least three, one per primary asset the profile writes, or the seed refuses the profile. |

## Initial tenant

A platform that boots holding no tenant at all creates one, after the platform seed when it is enabled. A platform that already holds a tenant is left alone, so changing these values afterwards does nothing. The check runs at every boot: a platform whose tenants were all deleted gets its initial tenant back at the next restart. The tenant is set up like one created from Platform › Tenants, with no member: XTM One's super administrator reaches it and adds the members. Set its id to write the [connectors](#connectors) configuration (`CONNECTORS__<NAME>__<tenant-uuid>__…`) before the first boot.

In Community Edition this is the platform's only tenant, since a second one is refused. Its name can be changed later from Platform › Tenants, its id cannot: having no member, the tenant is renamed by XTM One's super administrator, or by a holder of the Create Tenants platform capability once they are a member of it.

| Variable               | Required | Default Value                          | Architectural/Functional Impact                                                                  |
|:-----------------------|:--------:|:---------------------------------------|:--------------------------------------------------------------------------------------------------|
| `INITIAL_TENANT__ID`   |    No    | `01a043da-ff47-72c2-aaa4-7da56d85c83e` | UUID of the initial tenant. A value that is not a UUID fails the boot parse.                      |
| `INITIAL_TENANT__NAME` |    No    | `Default`                              | Name of the initial tenant, trimmed. A blank value fails the boot parse.                          |

If creating the tenant fails, the failure is logged and the platform boots without it; a super administrator can still create one from Platform › Tenants.

## Platform

| Variable                              | Required | Default Value | Architectural/Functional Impact                                                                                                                                                                                                              |
|:--------------------------------------|:--------:|:--------------|:---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `PLATFORM__RESET_ON_VERSION_CHANGE`   |    No    | `false`       | When `true`, the database is fully reset (`DROP SCHEMA public CASCADE` inside a transaction) on boot if `APP_VERSION` differs from the last stored version. Requires `APP_VERSION` env var to be set. The platform then re-runs migrations and seeds from scratch. The instance identifier stored in `platform_metadata` is captured before the drop and restored after migrations: the XTM license sub-licenses the platform by it, so regenerating it would silently downgrade the platform to Community Edition. An installed Filigran license is preserved the same way. |

## Feature Flags (development)

Feature flags let an unfinished feature be merged into `main` behind an off switch, then enabled on a staging deployment, in CI or in e2e without cutting a release. They are **deployment-level**, not per user and not per tenant: there is no targeting, no percentage rollout, no administration UI and no runtime toggling — a flag is turned on by a restart.

Flag names come from a closed registry, `utils/src/feature-flags.ts`, shared by the backend and the frontend. A name absent from that registry **fails the boot parse** rather than being silently ignored. The special flag `*` turns every flag on at once, which is what a dev stack or a CI job normally wants.

Every flag is **off by default** and **temporary**. When its feature ships globally, delete the entry from the registry: every gate still naming it stops compiling, which is how the gates get removed along with it. A flag that has outlived its feature is a bug, not a configuration option.

The backend resolves a flag with `isFeatureEnabled(flag)` and gates an endpoint with `procedure.use(featureFlagged(flag))`, which answers `FORBIDDEN` while the flag is off. A whole backend feature is held back by declaring `featureFlag` on its `registerFeature()` entry; boot then never activates it, and since a feature registers its router from inside `activate()`, its endpoints answer `NOT_FOUND` rather than `FORBIDDEN`. Only the **enabled** list is published to the browser, on the public `authStatus` bootstrap payload — a client learns which features are live, never which unreleased ones exist. The frontend reads it with the `useFeatureFlag(flag)` hook or wraps the gated UI in `<FeatureFlagged flag="...">`, which hide an **element**, not a route: a gated page stays reachable by direct URL, so the tRPC gate is what actually defends the feature.

| Variable         | Required | Default Value | Architectural/Functional Impact                                                                                                                                                                                                             |
|:-----------------|:--------:|:--------------|:-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `FEATURE_FLAGS`  |    No    | `[]`          | Comma-separated list of enabled flags (e.g. `FEATURE_FLAGS=*`). Each value must be a name declared in the registry, or `*`. The indexed form `FEATURE_FLAGS__0`, `FEATURE_FLAGS__1`, … also works; in a JSON config file this is the `feature_flags` array. |
## Enterprise Edition

OpenCRQ ships as **Community Edition** by default. Some features are reserved for **Enterprise Edition** — multi-tenancy and the Ask Ariane assistant today; each one says so where it appears, and the backend enforces the entitlement on every EE endpoint.

The entitlement comes from **the customer's Filigran license**, in one of two forms. The first: OpenCRQ registers with the connected XTM One deployment, which returns the Filigran license covering this instance (`xtm_license_pem`) when there is one. The second, described under [Filigran license installed on the instance](#filigran-license-installed-on-the-instance), is the same certificate installed on OpenCRQ by a platform administrator. OpenCRQ verifies that certificate itself, against the XTM CA compiled into its build — the signature, the `filigran xtm` product, the license type and its validity dates, and an OpenCRQ sub-license naming this instance's `instance_id` or `global` — and grants Enterprise Edition only when every check passes. The `ee_enabled` flag of the same answer is advisory and grants nothing on its own, whoever sends it. There is no license file and no environment variable.

**This requires an XTM One release that returns the license** with the registration: the first release after 1.260917.0. An older XTM One answers `ee_enabled` without it: the platform then stays in Community Edition — a multi-tenant deployment keeps a single reachable tenant — and logs a warning naming the upgrade. **Upgrade XTM One first, then OpenCRQ**: an older OpenCRQ ignores the license and keeps its edition against a newer XTM One.

Being remote, the entitlement fails closed. The license is verified again on every 5-minute heartbeat, and each verified answer grants Enterprise Edition for 7 days from that instant, or until the license stops entitling the platform if that comes first. An answer that proves nothing — no license, or one that does not verify — revokes it at once, and a persistent outage lets the grant run out rather than keeping the last known answer alive indefinitely. A `standard`, `lts` or `nfr` license entitles until 90 days past its expiry date, a `trial` license until its expiry date, and a `ci` license for 45 minutes from the creation of this instance at most. The grant lives in memory only, so a restart always starts from Community Edition until the next successful registration — independent of how long XTM One itself stays unreachable, which has its own, much shorter grace window (see [XTM One](#xtm-one-ai-assistant)). The registration fires as soon as the engines start rather than on a delay, because until it answers an entitled platform serves Community Edition — and a gate waits up to 5 seconds for that first answer instead of reading the not-yet-decided default. Past that budget the platform serves Community Edition and the answer corrects it whenever it lands.

The instance is identified to XTM One by its `instance_id`, held in `platform_metadata` and preserved across a `reset_on_version_change`: regenerating it would present a platform the license does not name. `instance_creation` is **not** preserved, so a reset restarts a `ci` license's 45-minute window, as OpenCTI does when its database is recreated.

**Settings → Parameters** carries the Filigran license and XTM One cards, held behind the `enterprise-edition-status` feature flag (see [Feature Flags](#feature-flags-development)), beside the Tenant and OpenCRQ platform cards, which give platform administrators the instance identifier XTM One registers this platform under. The Filigran license card says which edition the platform runs and where that answer comes from, the license type, and the date the license itself expires (before the 90-day grace of a `standard`, `lts` or `nfr` license, marked as such once past). With an XTM One configured, the license is installed there: a Community platform is sent to XTM One's platform settings to install it, and the card says why a platform connected to XTM One stays in Community Edition (XTM One is too old to return the license, withholds it, returns none, or returns one that does not verify). Without one, the card says OpenCRQ takes its Filigran license from XTM One: there is no way to install a license on OpenCRQ itself (see below). A license installed on the instance before 17 November 2026 is still listed with **Remove**; the Enterprise Edition XTM One grants cannot be removed from OpenCRQ, and the card is read-only for everyone else. The XTM One card gives the connection state, when XTM One last answered a registration and, while XTM One is unreachable, its registration is refused or its answer cannot be read, since when and what to check (an unreadable answer names the `XTM_ONE__URL` and the XTM One and OpenCRQ versions). For a platform administrator it also carries **Check now**, which registers with XTM One at once rather than at the next 5-minute heartbeat, so a license just installed in XTM One applies without waiting, and shows that attempt's own outcome, a refusal or an outage included, even within the ten-minute grace the XTM One toast waits for. What the outage takes away depends on the edition: a Community platform loses Ask Ariane and license activation; a platform entitled by XTM One is told Ask Ariane is unavailable and Enterprise Edition is no longer extended, with the date it ends (or the next restart); a platform entitled by a Filigran license installed on the instance is told Ask Ariane is unavailable, Enterprise Edition carrying on until that license's own end. The entitlement itself is never flagged: a deployment with the flag off still resolves, enforces and reports its edition.

### Filigran license installed on the instance (before 17 November 2026)

Since Filigran License v2 (17 November 2026), XTM One is mandatory for OpenCRQ's Enterprise Edition: the Filigran license is installed in XTM One, which distributes it to OpenCRQ at registration. OpenCRQ has no licence input of its own any more: no field in the UI, no API procedure (the `edition.installFiligranLicense` procedure is removed) and no environment variable or configuration key.

A license a platform administrator installed directly on OpenCRQ before that date keeps working as before: it is kept in `platform_metadata` (`filigran_license_pem`), preserved across a `reset_on_version_change` like `instance_id`, and verified again at every boot with the check the registration answer goes through (signed by the XTM CA, product `filigran xtm`, a valid type and dates with the 90-day grace of a `standard`, `lts` or `nfr` license, and an OpenCRQ sub-license, OID `1.3.6.1.4.1.62944.90`, holding `global` or this instance's `instance_id`). It grants Enterprise Edition until the license itself stops entitling the platform; **Remove** revokes it at once. XTM One's grant and such a license are independent: the platform runs Enterprise Edition while either entitles it, and the card names the form in use, *distributed by XTM One* or *installed on this instance*.

The current edition is exported as the `is_enterprise_edition` usage-telemetry gauge.

A development stack gets its edition the same way, from a license installed in the stack's own
XTM One: see [the development environment guide](../development/development.md#enterprise-edition-locally).

## XTM One (AI assistant)

Connecting OpenCRQ to an XTM One instance activates the embedded "Ask Ariane" AI assistant and registers the platform for agentic AI capabilities (native GRC agent tools, MCP server, intent routing). The integration follows the same inbound-registration contract as OpenCTI and OpenAEV: OpenCRQ registers itself with XTM One every 5 minutes (the registration doubles as the liveness heartbeat) and publishes an Ed25519 JWKS at `GET /xtm/auth/jwks` for mutual per-user JWT authentication. All chat traffic is proxied through the OpenCRQ backend — the browser never talks to XTM One directly.

XTM One is **required**, in Community as in Enterprise Edition: `XTM_ONE__URL`, `XTM_ONE__TOKEN` and `XTM_ONE__PLATFORM_URL` must all be set, or the platform refuses to start, naming whichever of the three is missing or blank. There is no `XTM_ONE__ENABLED` switch any more — the block cannot be turned off, and a leftover `XTM_ONE__ENABLED=false` is refused the same way (`XTM_ONE__ENABLED=true` is tolerated, for installations that still set it).

The AI assistant is an **Enterprise Edition** feature, exactly like the OpenCTI and OpenAEV Ask Ariane integrations. It reads the platform-wide entitlement described under [Enterprise Edition](#enterprise-edition), which this very integration is what resolves: the edition follows the XTM license XTM One returns and OpenCRQ verifies. Community deployments keep the full CE integration surface (registration, JWKS, agent tool access, `/mcp/opencrq` on the XTM One side); only the embedded chat is gated, and its launcher is not rendered at all, since the entitlement follows the XTM license and no self-service upgrade exists.

**Human-in-the-loop tool approval.** XTM One can require a person's consent before an agent runs a tool that is not whitelisted. The turn pauses and Ask Ariane shows the proposed call — tool, arguments and what each one means — with three verdicts: approve it once, decline it with a reason the agent receives and can adapt to, or approve that tool from then on, including on unattended scheduled runs. A page reload does not strand a prompt: the panel re-reads what the paused turn is still waiting on. OpenCRQ declares that it can prompt only while the assistant is Enterprise Edition entitled — without the entitlement the chat is unreachable, so there would be nobody to ask and XTM One leaves the decision on OpenCRQ-contributed tools to an XTM One administrator.

| Variable                 | Required | Default Value | Architectural/Functional Impact                                                                                                              |
|:-------------------------|:--------:|:--------------|:----------------------------------------------------------------------------------------------------------------------------------------------|
| `XTM_ONE__URL`           |   Yes    | -             | URL this OpenCRQ backend reaches XTM One on (e.g. `https://xtm-one.example.com`, or an internal address such as `http://xtm-one:4000`). See below. |
| `XTM_ONE__TOKEN`         |   Yes    | -             | Shared platform registration token (`PLATFORM_REGISTRATION_TOKEN` on the XTM One side). Pass through a secret store. XTM One withholds the XTM license while it runs with the published default `PLATFORM_REGISTRATION_TOKEN`: set a token of your own. |
| `XTM_ONE__PLATFORM_URL`  |   Yes    | -             | Public base URL of this OpenCRQ instance — used as the JWT issuer, the JWKS audience, and the `platform_url` sent at registration.            |

Once configured, an unreachable or rejecting XTM One does not fail the healthcheck — the process stays up and serves everything that does not depend on XTM One — but the application shows a persistent toast, seen by every user, naming the cause: **not answering** (network error, timeout, or any non-2xx status other than 401/403) as soon as the first registration attempt since boot fails, or once attempts still fail when the last successful one is at least ten minutes old (twice the 5-minute registration heartbeat, independent of the Enterprise entitlement's 7-day validity above); or **refused** when the registration is answered with HTTP 401 or 403, under the same threshold, usually a token that does not match `PLATFORM_REGISTRATION_TOKEN` on XTM One, though a proxy in front of it answers the same way; or **unreadable** when XTM One answers `200` with a body OpenCRQ cannot read (not JSON, or not the expected answer), under the same threshold, so the toast says the registration answer cannot be read — usually `XTM_ONE__URL` pointing at something other than XTM One, a proxy or captive-portal page in front of it, or an XTM One version this OpenCRQ does not understand. Neither a refusal nor an unreadable answer revokes the Enterprise grant, which lapses on its own end like during an outage. An attempt that fails before reaching XTM One, on the database, counts for nothing. Whichever the cause, the toast says what the outage takes away, by edition, in the same words as the Filigran license card: on a platform entitled by XTM One, Ask Ariane is unavailable and the grant is no longer extended, so Enterprise Edition ends on the grant's end date, or at the next restart until XTM One answers again, since that grant lives in memory only (see [Enterprise Edition](#enterprise-edition)); on a platform entitled by a Filigran license installed on the instance, only Ask Ariane is unavailable, the license being verified again at every boot; on a Community platform, XTM One cannot grant Enterprise Edition. The toast clears on its own, without a page reload, once a registration succeeds again, comes back at the next 5-minute poll if it was closed or replaced by another toast while the outage holds, and links to the Filigran license card (behind the `enterprise-edition-status` flag) for detail.

**Procedure manifest.** XTM One calls OpenCRQ's tRPC procedures by name and needs to know which ones this instance serves and what input each accepts, since an OpenCRQ older or newer than the XTM One it talks to does not serve the same API. Every registration therefore carries `procedure_manifest_fingerprint`, a SHA-256 of the manifest of what this instance's `/trpc` serves. When XTM One does not hold that fingerprint it answers `procedure_manifest_wanted: true`, and OpenCRQ registers once more at once with the manifest itself (`procedure_manifest`), under the same registration token; a failed follow-up leaves the registration state as it was and the next heartbeat asks again. The same manifest, with its `fingerprint`, is served by the authenticated `procedureManifest` query (a user session or a service account), for an integration with no registration. During a rolling deployment of two versions, what XTM One holds follows whichever replica registered last until the rollout ends.

**Public and internal URLs.** XTM One signs its tokens with, and expects on the tokens sent to it, its public `BASE_URL`, and it publishes that identity at `GET /xtm/auth/metadata`. `XTM_ONE__URL` may therefore be an internal address (Docker service name, Kubernetes service): OpenCRQ reads the identity from that document on `XTM_ONE__URL`, trusts XTM One tokens issued under it, fetches XTM One's keys on `XTM_ONE__URL` only, addresses its own tokens to it, and hands it to the browser for the links that open XTM One (Ask Ariane, the MCP endpoint card, the CTEM command center). An XTM One that does not publish the document is known by `XTM_ONE__URL` alone, as before. The identity is read once an hour; a failed read keeps the last one and is retried after a minute. On the XTM One side, `OPENCRQ_API_URL` is the URL it reaches this OpenCRQ on when `XTM_ONE__PLATFORM_URL` is not reachable from its containers.

**Sign in with XTM One.** OpenCRQ signs its users in through XTM One only: it keeps no password, and has no other way in, development builds included. It is an OpenID Connect client of XTM One (authorization code with PKCE `S256`, `state` and `nonce`; the ID token is verified against XTM One's keys on `XTM_ONE__URL`). There is nothing to configure beyond the three variables above, which every build requires: OpenCRQ sends its callback, `XTM_ONE__PLATFORM_URL` + `/api/auth/xtm/callback`, with its registration, and XTM One answers the id of the client it keeps for this platform. Sign-in through XTM One is on once that answer has come, so it needs an XTM One that creates sign-in clients at registration and a `PLATFORM_REGISTRATION_TOKEN` of your own on the XTM One side: XTM One creates none with the published default token. The browser goes to the authorization endpoint XTM One advertises (its public `BASE_URL`); the code exchange and the keys go to `XTM_ONE__URL`.

- **Signing in**: opening OpenCRQ with no session shows its sign-in page, with "Sign in with XTM One" while XTM One answers, which OpenCRQ's backend checks when the page loads and again right before sending the browser there (an answer is kept 15 seconds, so a stopped XTM One shows within about 20 seconds). When a session of this browser ended, the page goes back through XTM One on its own, once. XTM One asks for no consent, so with an XTM One session the button brings the person back at once, signed in, on the page they asked for. While XTM One does not answer, OpenCRQ says "XTM One is unavailable" with "Try again"; there is no fallback.
- **Signing out** ends the OpenCRQ session and shows the sign-in page, which never sends the browser to XTM One on its own: the XTM One session, which OpenCRQ has no way to end, is still open, and "Sign in with XTM One" brings the same person back. Switching account means signing out of XTM One too.
- **A sign-in that did not go through** (cancelled in XTM One, interrupted, refused) shows why, with "Sign in with XTM One".
- **Who signs in**: an account is the XTM One user (its `sub`) it was created for, and always signs in as that XTM One user, whatever its email becomes; the email never finds an account. An XTM One user OpenCRQ does not know gets an account at their first sign-in, with no password and access to nothing: they see "You don't have access to OpenCRQ yet" until an administrator gives them a tenant. One whose email another OpenCRQ account already holds (case-insensitive) is refused and logged as a warning with the XTM One `sub`.
- **What XTM One says, at every sign-in**: the email, the full name and the title (the `xtm_title` claim, or XTM One's `GET /api/v1/auth/me` when the claim is not sent) replace OpenCRQ's, which shows them read-only; an email another account holds is left unchanged. The per-tenant name and title stay editable in OpenCRQ.
- **Super administrator**: XTM One's own platform administrator (its `ADMIN_EMAIL` account, signalled by the `xtm_super_admin` claim) is OpenCRQ's super administrator: every right in every tenant and on the platform, without being a member of any tenant or platform group, set and cleared at every sign-in and session renewal. It settles the first install and any lockout, from XTM One. XTM One's ordinary `admin` role gives nothing in OpenCRQ: platform groups, "Platform administrators" included, are managed in OpenCRQ. An XTM One that does not send the claim designates nobody.
- **Default platform groups**: a platform that starts with no platform role and no platform group creates two of each, with no member: the "Platform administrator" role (Create Tenants, Manage Users and Platform Administration Access) in the "Platform administrators" group, and the "User manager" role (Manage Users and Platform Administration Access) in the "User managers" group. XTM One's super administrator adds the first members, from Settings › Security › Groups, on the Platform scope. From then on they belong to the operator: one that is renamed, changed or deleted stays that way after a restart, and only deleting every platform role and group brings them back at the next boot. A platform that already holds a platform role or group, after an upgrade or with the platform seed, gets none. If creating them fails, the error is logged and the platform starts without them.
- **Sessions**: a session opened through XTM One lasts about an hour and renews on a navigation shortly before it ends, through XTM One and with no click while the XTM One session is open; an account disabled in XTM One cannot renew, so it loses OpenCRQ within the hour. A renewal XTM One refuses ends the OpenCRQ session at once and shows why, with "Sign in with XTM One"; one that fails for another reason (XTM One slow or down) leaves the session to run to its end.
- **One address**: a sign-in started on another host than `XTM_ONE__PLATFORM_URL`'s (a DNS alias, `127.0.0.1` for `localhost`) first moves the browser to the platform URL, since XTM One sends it back there (once; behind a proxy that rewrites the `Host` header, every sign-in pays that one extra redirect).
- **Requirements**: `XTM_ONE__PLATFORM_URL` must be HTTPS (or a loopback address in development): XTM One refuses any other callback, so OpenCRQ then sends none and logs a warning: nobody can sign in, while the rest of the registration (entitlement, Ask Ariane) is unaffected.
- **Hardening**: keep XTM One's self-service registration off (its default) or restricted to your domains.
- **Adding members**: an administrator adds people by email, with suggestions from XTM One's directory (`GET /api/v1/users/directory`), and OpenCRQ asks XTM One, as that administrator, to tell each one (`POST /api/v1/platform/access-notices`): XTM One emails a person who has an XTM One account, and invites one who has none when the administrator may invite (XTM One's administrators today); a refusal adds nothing for that email. An invited person's account waits with no XTM One id until they sign in through the single-use `/join/<token>` link their invitation leads to, with the same email: the email alone never ties an account to an XTM One user. When XTM One sends no email, the administrator gets the tenant's link to pass on.
- **Upgrading**: an instance whose configuration lists the retired `xtm-sign-in` flag in `FEATURE_FLAGS` refuses to boot until it is removed. Accounts made with a password before sign-in through XTM One and never signed in through it are not tied to an XTM One user: their owner's first sign-in is refused as `conflict` while the old account holds the email, until an operator ties it as below or removes it.
- **First install**: the platform starts with its [initial tenant](#initial-tenant), and XTM One's administrator, signing in as super administrator, lands in it, and can rename it from Platform › Tenants. An XTM One that does not send `xtm_super_admin` designates nobody, so an instance installed from scratch has its tenant but nobody who can add members to it.
- **An XTM One user recreated** (new `sub`) is another person to OpenCRQ: their old account keeps the email, so the new one is refused until an operator ties the old account to the new `sub` in the database (`UPDATE users SET xtm_subject = '<new sub>' WHERE email = ...`).

## Connectors

OpenCRQ can enrich supporting assets and vulnerabilities through external connectors, configured via the `connectors` config block. Credentials are never stored in the database: each connector is a **map keyed by tenant UUID**, so two tenants can point the same connector at different accounts/instances. The id of a platform's first tenant is known before it starts: see [Initial tenant](#initial-tenant). There is no `enabled` flag: a connector is active for a given tenant as soon as that tenant has an entry:
- **CISA** and **FIRST** need no configuration at all — they are always on, for every tenant.
- **NIST** works with no configuration, but runs in a rate-limited degraded mode for a tenant until that tenant's entry supplies an API key.
- **OpenCRQ Default Threats** needs no configuration either, and no service account: it is on for every new tenant, and each tenant switches it off from its Configure drawer. Its one setting is platform-wide, below.
- **Microsoft Graph**, **OpenAEV** and **Drata** stay inactive for a tenant (reported as not configured by `getConnectorsStatus`, sync jobs fail fast with a `NonRetryableError`) until that tenant has an entry with a full config block.

Once a tenant has an entry for a connector, an admin for that tenant still needs to link a service account to it from the Integrations UI before it is used.

Each field is its own flat environment variable, `CONNECTORS__<NAME>__<tenant-uuid>__<field>` — `nconf`'s env store splits on `__` and lower-cases each segment, so the tenant UUID (already lowercase hex) round-trips unchanged — e.g.:

```
CONNECTORS__OPENCTI__89c70e9a-8317-4da7-8765-1b9ba004c32c__URL=https://opencti.example.com
CONNECTORS__OPENCTI__89c70e9a-8317-4da7-8765-1b9ba004c32c__TOKEN=...
```

### NIST (National Vulnerability Database)

Powers the periodic CVE metadata sync (CVSS scores, descriptions) from the [NVD API](https://nvd.nist.gov/). The NVD catalog itself is shared across every tenant, but the API key is still per tenant: NIST attributes rate limits to the key, and each tenant's sync job runs independently. The key is optional but strongly recommended: the NVD rate limit is 5 requests/30s without a key vs. 50 requests/30s with one.

| Variable                                  | Required | Default Value | Architectural/Functional Impact                                                                        |
|:--------------------------------------------|:--------:|:--------------|:--------------------------------------------------------------------------------------------------------|
| `CONNECTORS__NIST__<tenant-uuid>__API_KEY` |    No    | -             | NVD API key for that tenant. Pass through a secret store. A tenant with no entry (or no `API_KEY`) syncs in degraded (lower rate-limited) mode. |

### Microsoft Graph (Microsoft Defender for Endpoint)

Syncs the devices and their vulnerability exposure, the identities, and the primary user of each workstation from Microsoft Defender for Endpoint, Entra ID and Intune via the Microsoft Graph API, authenticating with an Azure AD app registration using the OAuth2 client-credentials flow. A tenant activates once it has all three variables below set.

| Variable                                                    | Required | Default Value | Architectural/Functional Impact                                                                      |
|:--------------------------------------------------------------|:--------:|:--------------|:--------------------------------------------------------------------------------------------------------|
| `CONNECTORS__MICROSOFT_GRAPH__<tenant-uuid>__AZURE_TENANT_ID` | **Yes**  | -             | Azure AD tenant ID used to build that tenant's OAuth2 token endpoint (`login.microsoftonline.com/{tenant}/...`). |
| `CONNECTORS__MICROSOFT_GRAPH__<tenant-uuid>__CLIENT_ID`       | **Yes**  | -             | Client (application) ID of that tenant's Azure AD app registration.                                     |
| `CONNECTORS__MICROSOFT_GRAPH__<tenant-uuid>__CLIENT_SECRET`   | **Yes**  | -             | Client secret of that tenant's Azure AD app registration. Pass through a secret store.                  |

The app registration needs these Microsoft Graph **application** permissions, each granted with admin consent:

| Permission                                   | Required | What it reads                                                                                   | Without it                                                                                   |
|:---------------------------------------------|:--------:|:------------------------------------------------------------------------------------------------|:---------------------------------------------------------------------------------------------|
| `ThreatHunting.Read.All`                     | **Yes**  | Advanced Hunting: the devices, their vulnerabilities, the identities and the logon events.        | The sync fails.                                                                              |
| `Device.Read.All`                            | No       | Entra devices and their registered owners, which name the primary user of a workstation.         | The sync succeeds and no workstation gets a primary user.                                    |
| `DeviceManagementManagedDevices.Read.All`    | No       | Intune managed devices and their primary user, which takes precedence over the Entra owner and reaches devices Defender cannot tie to Entra. | Primary users come from Entra alone.                                                        |

The sync's last progress message says how each of the two optional sources answered: answered, refused (no consent), or unavailable with a reason, such as `400` on a tenant without an Intune licence, a throttled or failing service, or a timeout. A source that did not answer leaves the primary users it named as they were, and the sync carries on without it.

#### Devices imported from Microsoft Defender

Besides its name and address, each device brings its **operating system** (the release, such as `Windows 11 25H2` or `macOS Tahoe`, and its full version, such as `10.0.26200.9711`), its **vendor**, its **model** and its **subtype** (switch, router, smartphone, media device, and so on), the last of which refines the asset's subcategory. A device with no name is kept and shown under a name derived from its vendor, model and address (see [asset consolidation](asset-consolidation.md#an-asset-nobody-named)).

#### Identities imported from Microsoft Defender

Defender can list accounts that carry nothing but an internal object id. The sync skips every account that is **disabled** and has **no display name, no user principal name, no account name and no SID**: nothing in it could be read, matched or acted on. An account whose enabled state is unknown, or that carries any one of these, is imported as usual, and a known account that becomes disabled stays, marked disabled in its "Account enabled" field.

Each sync also removes what these accounts left behind. It deletes their Defender records, then deletes an asset only when Defender no longer reports it **and** nobody added anything to it: a value or properties entered by hand, a hand-set tag, a relation, a dependency (on a primary asset or on another supporting asset), a finding attached or set aside by a person, or a place in a risk scope. An asset that holds any of these stays, with the values it had, and is no longer updated by Defender.

The count is written at the end of the identity pass, in the log of the sync run, on the connector's **Logs** tab: `<n> disabled accounts with no identifying data skipped, <m> assets removed`. The tab keeps the last runs only (`JOBS__LOG_RETENTION_RUNS`).

### OpenAEV (Adversary Exposure Validation)

Syncs the asset inventory, the latest attack verdicts (prevention and detection per technique) and the CVEs from an [OpenAEV](https://github.com/OpenAEV-Platform/openaev) instance, to support attack-outcome and exposure analyses. A tenant activates once it has both variables below set: at boot, an existing tenant given them and with no account bound gets the account `OpenAEV (deployment)`, in its `connectors` group, bound to the integration, so it comes up enabled. An account an admin bound is never replaced; the binding stays editable from the Integrations page.

| Variable                                        | Required | Default Value | Architectural/Functional Impact                                     |
|:----------------------------------------------------|:--------:|:--------------|:-----------------------------------------------------------------------|
| `CONNECTORS__OPENAEV__<tenant-uuid>__URL`   | **Yes**  | -             | Base URL of that tenant's OpenAEV instance (e.g. `https://openaev.example.com`). |
| `CONNECTORS__OPENAEV__<tenant-uuid>__TOKEN` | **Yes**  | -             | API token used to authenticate against that tenant's OpenAEV API. Pass through a secret store. |

The connector reads OpenAEV's bulk snapshot export, which needs a version that ships it with its `BULK_SNAPSHOT_EXPORT` preview feature enabled, and a token whose role holds the `ACCESS_SNAPSHOT_OBSERVATION` and `ACCESS_ASSESSMENT` capabilities. Without the export, it falls back to reading exercises and findings, which needs `ACCESS_FINDINGS` instead. See [OpenAEV Connector](openaev-connector.md) for the phases, their cadences, the fallback and what a failed prerequisite looks like.

### OpenCTI (Live Stream import of STIX bundles)

Subscribes to an OpenCTI Live Stream (Server-Sent Events) to automatically keep threat intelligence (`threat_analyses`) up to date, as an alternative to manually uploading STIX bundle files. Each tenant independently picks which Live Stream to subscribe to and can enable/disable it, against its own OpenCTI instance.

| Variable                                        | Required | Default Value | Architectural/Functional Impact                                     |
|:----------------------------------------------------|:--------:|:--------------|:-----------------------------------------------------------------------|
| `CONNECTORS__OPENCTI__<tenant-uuid>__URL`   | **Yes**  | -             | Base URL of that tenant's OpenCTI instance (e.g. `https://opencti.example.com`). |
| `CONNECTORS__OPENCTI__<tenant-uuid>__TOKEN` | **Yes**  | -             | API token used to authenticate against that tenant's OpenCTI Live Stream/GraphQL API. Pass through a secret store. |

OpenCRQ drives the Live Stream itself: at every start, for each tenant given these variables, it finds the stream it manages in that OpenCTI (named "OpenCRQ threat feed", recognised by a marker in its description), creates it with the filter of [OpenCTI Connector](opencti-connector.md) when there is none, and brings its filter back to the current version when it carries an older one. A tenant with nothing bound is then bound to it, under the "OpenCTI (deployment)" service account, and the connector starts. A binding an admin made is never replaced, and **Configure** still picks any other stream. Binding at start is safe because every mirrored object keeps its OpenCTI markings and each person reads only what their markings allow.

The token needs read access on every marking definition in scope and the `TAXIIAPI_SETCOLLECTIONS` capability (Manage CTI data sharing > Manage Live Streams), or `BYPASS`. Without that capability nothing is written in OpenCTI, the start goes on, and the connector stays idle with a message naming the missing capability until it is granted (then restart) or an admin picks a stream.

See [OpenCTI Connector](opencti-connector.md) for the setup required on the OpenCTI side: the Live Stream filter this connector needs, and the access its token must have.

### OpenCRQ Default Threats (public threat groups and campaigns)

Loads the live MITRE ATT&CK groups and campaigns into a tenant's threats, so its scenarios compute before it has a threat feed of its own. The file is published in [OpenCRQ-Platform/datasets](https://github.com/OpenCRQ-Platform/datasets), derived from MITRE ATT&CK and following its releases; a sync downloads it only when it changed (`ETag`, then `Last-Modified`), and loads it again after an upgrade of OpenCRQ, whose technique catalogue may know techniques the previous load left out. Switching the connector off for a tenant removes its threats, and only them. On an existing deployment, a tenant that has OpenCTI configured starts with it off.

A group or campaign the file gives judged probabilities of leading to each attack outcome is weighed on those, in place of OpenCRQ's own estimate; any other is estimated as any other threat is.

ATT&CK documents mostly state-sponsored groups, so cybercrime is under-represented among these threats.

| Variable                                     | Required | Default Value | Architectural/Functional Impact |
|:---------------------------------------------|:--------:|:--------------|:--------------------------------|
| `CONNECTORS__DEFAULT_THREATS__DATASET_URL`   |    No    | `https://raw.githubusercontent.com/OpenCRQ-Platform/datasets/main/data/` | The folder the connector reads `default-threats-v1.json` from, for every tenant. An air-gapped platform points it to a mirror of that folder. Set it empty to turn the connector off for every tenant, which then reads "Unavailable". |

### Drata (compliance posture import)

Mirrors a tenant's security frameworks (ISO 27001:2022 and SOC 2) with their requirements and DCF control readiness from the [Drata Public API v2](https://developers.drata.com/). A tenant activates once it has an entry with an API key.

| Variable                                              | Required | Default Value                    | Architectural/Functional Impact                                     |
|:----------------------------------------------------------|:--------:|:---------------------------------|:-----------------------------------------------------------------------|
| `CONNECTORS__DRATA__<tenant-uuid>__API_KEY`      | **Yes**  | -                                | That tenant's Drata API key with read scopes on Frameworks and Controls. Pass through a secret store. If the key restricts source IPs, allowlist this deployment's egress IP. |
| `CONNECTORS__DRATA__<tenant-uuid>__URL`          |    No    | `https://public-api.drata.com`   | Drata API base URL for that tenant. EU-hosted accounts must set `https://public-api.eu.drata.com` (the key is rejected with a 401 on the wrong regional instance), APAC ones `https://public-api.apac.drata.com`. |
| `CONNECTORS__DRATA__<tenant-uuid>__WORKSPACE_ID` |    No    | `1`                              | Drata workspace to read for that tenant. Accounts without Multiple Product Support always use workspace `1`. |
