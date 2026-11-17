# 🐋 Docker Configuration

This guide explains how to use the Docker configuration located in the `docker/` directory to build and run OpenCRQ (Backend + Frontend) in a production-like environment.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) or [Podman](https://podman.io/getting-started/installation)
- [Docker Compose](https://docs.docker.com/compose/install/) or `podman compose`

## Configuration

1. Navigate to the docker directory:
   ```bash
   cd docker
   ```

2. Copy the sample environment file:
   ```bash
   cp .env.sample .env
   ```

3. Edit `.env` to configure your environment. See [Configuration Guide](configuration.md) for details. `XTM_ONE__URL`, `XTM_ONE__TOKEN` and `XTM_ONE__PLATFORM_URL` are required: the platform refuses to start without them (see [XTM One](configuration.md#xtm-one-ai-assistant)).

## Usage

### Start the application

Build and start the containers:

**Using Docker:**
```bash
docker compose up --build --pull always -d
```

**Using Podman:**
```bash
podman compose up --build --pull=always -d
```

The application will be available at `http://localhost:<HTTP_SERVER__PUBLIC_PORT>` (`3020` in the shipped `.env.sample`; Compose only falls back to `3002` if the variable is left unset).

### Upgrade from PostgreSQL 16

The database runs PostgreSQL 17 (`pgvector/pgvector`, the XTM bundle's image), in the `postgres17_data` volume. An installation created when this file ran PostgreSQL 16 keeps its data in `postgres_data`, which PostgreSQL 17 cannot open, so the first `up` of this version starts on an empty database. Move the data before that `up`, from the `docker` directory:

```bash
set -a; . ./.env; set +a
docker compose down

# Dump the roles and every database from a PostgreSQL 16 server started on the former volume.
docker run -d --name opencrq-pg16 -v opengrc-prod_postgres_data:/var/lib/postgresql/data postgres:16-alpine
until docker exec opencrq-pg16 pg_isready -U "$POSTGRES__USER"; do sleep 1; done
docker exec opencrq-pg16 pg_dumpall -U "$POSTGRES__USER" > opencrq-pg16.sql
docker rm -f opencrq-pg16

# Start PostgreSQL 17 alone and load the dump.
docker compose up -d postgres
until docker compose exec postgres pg_isready -h localhost -U "$POSTGRES__USER"; do sleep 1; done
docker compose exec -T postgres psql -U "$POSTGRES__USER" -d postgres < opencrq-pg16.sql

docker compose up -d
```

The load reports that the role and the database named after `POSTGRES__USER` already exist: the new server created them, and the dump fills that database. With Podman, write `podman` for `docker` and `podman compose` for `docker compose`.

If the first `up` of this version already ran, the application set up a new platform in the empty database. Remove it with `docker compose down` and `docker volume rm opengrc-prod_postgres17_data` before the steps above.

`postgres_data` is left as it was. Once the application shows its data again, `docker volume rm opengrc-prod_postgres_data` reclaims it.

### Stop the application

**Using Docker:**
```bash
docker compose down
```

**Using Podman:**
```bash
podman compose down
```

### View logs

**Using Docker:**
```bash
docker compose logs -f
```

**Using Podman:**
```bash
podman compose logs -f
```
