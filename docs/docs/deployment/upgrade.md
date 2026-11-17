# Upgrade

This page covers the upgrade and rollback procedures for a self-hosted XTM One instance. Filigran SaaS instances are upgraded by Filigran.

## Before you upgrade

1. Read the notes of the versions between yours and the target: the [release notes](https://github.com/XTM-One-Platform/xtm-one/releases), and [UPGRADING.md](https://github.com/XTM-One-Platform/xtm-one/blob/main/UPGRADING.md), which lists the releases that need an action or change existing behaviour (a data migration, a new default, a variable to set).
2. Check the [deprecated environment variables](#deprecated-environment-variables) below and rename the ones you still use.
3. Take a backup of the database and the object storage at the same point in time, and keep your `.env` with it (see [Backup and restore](backup-and-restore.md)). Migrations run on startup and can change the schema, so this backup is your only way back.

## Procedure

Apply the update based on your deployment option:

**Pre-built images (Option B):**

```bash
docker compose pull
docker compose up -d
```

**Source-based (Option A):**

```bash
git fetch --tags
git checkout <new-version-tag>
docker compose build --pull
docker compose up -d
```

Migrations run automatically on startup. Wait for `/api/health/ready` to return 200 before verifying.

Image tags follow the format `<major>.<YYMMDD>.<patch>` and are listed on [Docker Hub](https://hub.docker.com/u/xtmone). A tag can also carry an `-lts.<n>` suffix (for example `1.260731.0-lts.1`), which marks a long-term support release.

## Rolling upgrade on Kubernetes

With several platform replicas, or on Kubernetes, you can upgrade without stopping the service:

- **Migrations run on the first new pod to start**, while the previous version still serves. A database lock lets one process migrate at a time; the other new processes wait, then start. Every migration is written to stay compatible with the version before it, which is what makes this safe. See [Scaling model](installation.md#scaling-model).
- **The platform drains before it stops.** A stopping platform pod lets its running chat turns finish for `GRACEFUL_TIMEOUT` (120 seconds by default). Give the pod a `terminationGracePeriodSeconds` above `GRACEFUL_TIMEOUT` plus 15 seconds, and use `maxUnavailable: 0` with `maxSurge: 1` so the old pod serves until the new one is ready (see [Kubernetes and Helm](installation.md#kubernetes-and-helm)).
- **The worker finishes its jobs before it stops**, for `WORKER_SHUTDOWN_GRACE_PERIOD` (20 seconds by default). Keep the worker's own stop grace comfortably above it (about 10 seconds more) so it can also run its shutdown step.
- **Interrupted work resumes.** An assignment run or a background task cut by the rollout is resumed by a running process; `xtmone_run_resumes_total` counts them per build (see [Monitoring](monitoring.md#runs-and-resumes)).
- **Count the surge pod in the connection budget**: one extra platform pod runs next to the others until the old one is gone (see [Configuration](configuration.md#performance)).

## Deprecated environment variables

Environment variables renamed by a release keep working under their old name for a while, so an upgrade does not silently change what your deployment does. While an old name is in use, XTM One logs a warning naming the replacement. Rename them in your compose files, Helm values and CI secrets before the removal date.

| Old name | New name | Read until | Notes |
|---|---|---|---|
| `OPENGRC_API_URL` | `OPENCRQ_API_URL` | 2026-12-31 | Renamed with the OpenGRC -> OpenCRQ product rename. When both are set, `OPENCRQ_API_URL` wins. See [Network reachability](xtm-suite-integration.md#network-reachability). |
| `CUSTOM_TOOL_DOCKER` | `CUSTOM_TOOL_SANDBOX` | Not scheduled | `CUSTOM_TOOL_DOCKER=true` is read as `CUSTOM_TOOL_SANDBOX=docker`, and only while `CUSTOM_TOOL_SANDBOX` is not set at all: any value of `CUSTOM_TOOL_SANDBOX`, `auto` included, wins. `docker-compose.yml` does not pass `CUSTOM_TOOL_DOCKER` to the containers. |

### Removed environment variables

These variables are no longer read. A deployment that still sets them gets no warning: remove them, or move to what replaces them.

| Variable | Replaced by |
|---|---|
| `VIDEO_STUDIO_PRIVATE_TARGETS` | `BROWSER_PRIVATE_TARGETS` (also in `Settings` → `Security` → `Security options`). A value saved in `Security options` moves over by itself; a variable must be renamed. See [Browser profiles](configuration.md#browser-profiles). |
| `VIDEO_STUDIO_RENDER_KEEP_LAST`, `VIDEO_STUDIO_RENDER_RETENTION_DAYS` | A platform setting, `Render retention`, in `Settings` → `AI Models` (shown while the Video Studio is enabled). |

## Version notes

### OpenSandbox server (Docker Compose)

The `opensandbox` service of `docker-compose.yml` is opt-in again: it starts only with the `opensandbox` profile. A deployment that does not use OpenSandbox has nothing to set; remove the container an earlier release started (it restarts in a loop when `OPENSANDBOX_API_KEY` is unset) with `docker compose --profile opensandbox rm -sf opensandbox`. A deployment that uses it sets `COMPOSE_PROFILES=opensandbox` and `OPENSANDBOX_API_KEY` in `.env` before `docker compose up -d`. See [Running the OpenSandbox server itself](configuration.md#running-the-opensandbox-server-itself).

`docker-compose.yml` now forwards `CUSTOM_TOOL_SANDBOX` and the `OPENSANDBOX_*` connection variables of `.env` to the platform and the workers; before, they never reached a container and the platform stayed on `auto`. Check your `.env` before upgrading: a `CUSTOM_TOOL_SANDBOX=opensandbox` line (`.env.sample` ships one) now takes effect, so either enable the `opensandbox` profile or remove the line. An override file that set these variables for the `platform`, `worker` and `worker-preview` services can drop them.

### Code interpreter sandbox workspace

From this release the code interpreter keeps a [sandbox workspace](../usage/sandbox-workspace.md) per conversation and per assignment run. Nothing has to be done on upgrade: a migration adds two tables (`sandbox_workspaces`, `sandbox_workspace_files`), and a cell that worked before works unchanged.

- **Storage.** No volume to provision: workspace files live in the platform bucket under `sandbox-workspaces/`, their manifest in PostgreSQL, so every backend replica and worker sees the same workspaces. Budget the bucket for the workspaces in use (2 GiB at most each by default, usually far less) and, with the local sandbox, the backend pods' temporary directory for the per-process cache (`CODE_INTERPRETER_WORKSPACE_CACHE_MB`, 2 GiB by default). See [Storage sizing](configuration.md#code-interpreter-sandbox-workspace).
- **Clean-up.** A sweep runs in the worker every hour (once for the whole install): it deletes the workspaces of archived or deleted conversations and deleted runs, the workspaces unused for `CODE_INTERPRETER_WORKSPACE_IDLE_TTL_HOURS` (7 days by default), and objects left under a prefix no workspace owns. Archiving or deleting a conversation from the chat also deletes its workspace at once.
- **Kernels.** `session=true` runs a long-lived kernel: a child process of the backend on the local sandbox, a container of its own on OpenSandbox (kept for `CODE_INTERPRETER_KERNEL_IDLE_SECONDS`, 15 minutes by default, and found again from any replica).
- **Packages.** Installs stay off until you set `CODE_INTERPRETER_PACKAGE_ALLOWLIST` (and, for a mirror, `CODE_INTERPRETER_PACKAGE_INDEX_URL`).
- **Graceful stops.** During a deployment, a chat turn whose worker is stopping now ends at its next step once less than a minute (or half of `GRACEFUL_TIMEOUT`) of the grace is left, its completed tool calls kept for "Continue where you left off". A turn that would have finished within the grace is not affected.
- **Turning it off.** `CODE_INTERPRETER_WORKSPACE_ENABLED=false` restores the stateless code interpreter.

### Platform registration token (Docker Compose)

This note applies when `PLATFORM_REGISTRATION_TOKEN` is not set in your `.env`, or is set to `xtm-default-registration-token`: Docker Compose then runs XTM One with that published default, which authenticates nobody. After upgrading such a deployment:

- The OpenCTI, OpenAEV and OpenCRQ platforms already registered keep their heartbeat, but the published default can no longer register a new platform, change the URL of a registered one, rename one or unregister one: a registered platform's URL is trusted to sign XTM One users in, and its name is what the agents bound to it read. Nor does its heartbeat change the intents and business vertical a platform declares (those of an upgraded platform are recorded once you set your own token), or replace a platform's license with anything but its renewal.
- The Filigran license certificate is no longer sent to the connected platforms, so a platform whose Enterprise Edition comes from your Filigran license (a sub-license) cannot prove it. The registration answer says why: `xtm_license_withheld_reason: registration_token_published_default`.

To leave the published default:

1. Generate a token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Set it as `PLATFORM_REGISTRATION_TOKEN` in the `.env` of XTM One, and set the same value as the registration token of every connected OpenCTI, OpenAEV and OpenCRQ platform. A platform still presenting the old value is refused.
3. Restart XTM One (`docker compose up -d`) and the platforms.
4. Check the [security audit](../administration/settings.md#security-audit) (`Settings` → `Security`, or `GET /api/v1/security-audit`): the platform registration token finding now reads `pass`. Then review the registered platforms and purge any you do not recognise: earlier releases let anyone holding the published default register one, and changing the token does not end the trust a registration carries. The integration catalog in Settings lists the OpenCTI, OpenAEV and OpenCRQ registrations; the security audit names any registration under another identifier with its id, which `DELETE /api/v1/platform/registrations/{id}/purge` removes.

## Rollback

!!! warning
    Migrations run automatically on startup and may alter the database schema. Once a migration has run, the previous application version may not work against the new schema. Rollback requires restoring the database **and** object storage from the same pre-upgrade recovery point.

1. `docker compose down`
2. Restore the database from the pre-upgrade backup.
3. Restore Silo/S3 to the same recovery point.
4. Revert image tags to the previous version.
5. `docker compose up -d`

The commands for each step are in [Backup and restore](backup-and-restore.md#restore).

## Next step

After the upgrade, check the [health probes and metrics](monitoring.md), and the [security audit](../administration/settings.md#security-audit) for any new finding.
