# Backup and restore

This page covers what to back up on a self-hosted XTM One instance, how to restore it, and how to copy an instance to another host. Filigran SaaS instances are backed up by Filigran.

The commands below use the service names of the Docker Compose stack (`postgres`, `minio`, `platform`, `worker`, `worker-preview`) and its default database name and user (`copilot`). Adapt them to your `DATABASE_URL` and storage settings, or use your managed services' own backup tools.

## What to back up

| What | Why | Where it lives |
|---|---|---|
| The PostgreSQL database | Every agent, assignment, run, conversation, user, setting and encrypted credential, and the knowledge-base embeddings (the `vector` extension, pgvector) | The `postgres` service, `pgdata` volume |
| The object storage bucket | Uploaded files, knowledge-base documents, large trace payloads and their images, sandbox workspace files, brand assets | The `minio` service (Silo), `minio-data` volume, or your S3 bucket (`S3_BUCKET`, `copilot-files` by default) |
| `SECRET_KEY` and the rest of `.env` | `SECRET_KEY` encrypts every stored credential and two-factor secret, and signs sessions | Your `.env`, or your secret manager |

!!! warning "Keep `SECRET_KEY` with every backup"
    A database restored without its original `SECRET_KEY` starts, but every encrypted value in it is unreadable: integration and LLM provider credentials, channel bot tokens, secret variables, OAuth clients, two-factor enrollments. There is no way to recover them. Store `SECRET_KEY` apart from the database backup, in a place you can reach when the host is gone.

Redis does not need a backup. Losing it costs only short-lived data:

- background jobs queued and not yet picked up by a worker. A knowledge-base document left waiting is queued again by the scheduler's recovery sweep;
- invitation and password reset links not used yet, which must be sent again;
- XTM One Desktop sign-ins in progress, rate-limit counters, and the last activity of each session (the idle timeout starts counting again).

## A consistent recovery point

The database and the bucket refer to each other: a row points at a file, a trace at its offloaded payload. Back them up at the same moment, or a restored row can point at a file that is missing.

The simplest way is to stop everything that writes, back up both, then start again:

```bash
docker compose stop platform worker worker-preview
```

On Kubernetes, scale the platform and the worker to zero for the same effect. If you cannot stop the service, back up the database first and copy the bucket right after, to keep the gap between them as short as possible.

## Back up the database

`pg_dump` in custom format keeps the schema, the data and the `vector` extension:

```bash
docker compose exec -T postgres pg_dump -U copilot -d copilot -Fc > xtm-one-db.dump
```

## Back up the object storage

With an external S3 bucket, use your provider's versioning, replication or snapshot features, or copy the bucket with any S3 client.

With the Silo service of the Compose stack, archive its volume while it is stopped. Compose prefixes volume names with the project name (by default the name of the folder holding `docker-compose.yml`); `docker volume ls` shows the exact name.

```bash
docker compose stop minio
docker run --rm -v <project>_minio-data:/data:ro -v "$PWD":/backup alpine \
  tar czf /backup/xtm-one-objects.tgz -C /data .
docker compose up -d
```

## Restore

Restore onto the same XTM One version as the backup, or a later one: on startup, a later version migrates the restored database forward. An earlier version cannot read a database a later one has migrated.

1. Put back the `.env` of the backed-up instance, with its `SECRET_KEY`, and set the image tags to the version you restore onto.
2. Stop the platform and the workers if they run, and start the storage services only:

    ```bash
    docker compose stop platform worker worker-preview
    docker compose up -d postgres minio
    ```

3. Replace the database with the backup:

    ```bash
    docker compose exec -T postgres dropdb -U copilot --if-exists copilot
    docker compose exec -T postgres createdb -U copilot copilot
    docker compose exec -T postgres pg_restore -U copilot -d copilot --no-owner < xtm-one-db.dump
    ```

    With your own PostgreSQL server, make sure the pgvector extension is installed on it: the dump re-creates the `vector` extension and needs it to be available.

4. Replace the objects with the backup:

    ```bash
    docker compose stop minio
    docker run --rm -v <project>_minio-data:/data -v "$PWD":/backup alpine \
      sh -c "find /data -mindepth 1 -delete && tar xzf /backup/xtm-one-objects.tgz -C /data"
    ```

5. Start the platform. Migrations run on startup if the version is later than the backup's.

    ```bash
    docker compose up -d
    ```

## Copy an instance to another host

A copy is a restore on another host: the same backup, the same `SECRET_KEY`, the same procedure. What differs is the instance's identity.

- **The instance ID.** It is stored in the database, so a restored copy has the same instance ID as the original. `PLATFORM_ID` forces it: set it to keep the identity stable across restores and re-installs, or to a new UUID to make a copy a separate instance (see [Configuration](configuration.md#xtm-hub-registration)).
- **The XTM Hub registration.** It is bound to the instance ID. A copy that keeps the ID keeps the registration. A copy with a new `PLATFORM_ID` shows `Register again` in `Settings` → `Parameters`, and sends nothing to the XTM Hub until you register it (see [XTM Hub registration](../administration/settings.md#xtm-hub-registration)).
- **Connected platforms and integrations.** A copy holds the original's registered OpenCTI, OpenAEV and OpenCRQ platforms, its integrations and their credentials, and its active assignments and flows. Once started, it runs them against the same systems as the original. For a test copy next to a live instance, start it where it cannot reach those systems, or press the [emergency stop](../administration/emergency-stop.md) with the `Whole platform` scope as soon as you sign in.
- **Its address.** Set `BASE_URL` (and `FRONTEND_URL`) to the copy's own address, and update the redirect URIs registered in your identity provider if the copy uses single sign-on (see [Authentication](authentication.md)).

## Verify after a restore

- `/api/health/ready` returns 200 (see [Monitoring](monitoring.md#health-probes)).
- You can sign in, and the bootstrap administrator is the one set in `.env`.
- An integration or an LLM provider answers its connection test: its credential decrypts, so `SECRET_KEY` is the right one. The [security audit](../administration/settings.md#security-audit) names any configuration it cannot decrypt.
- A knowledge-base document or an attached file opens: the bucket matches the database.
- `Settings` → `Parameters` shows the XTM Hub registration in the state you expect.

## Next step

Before your next version change, read [Upgrade](upgrade.md): its rollback procedure relies on the backup described here.
