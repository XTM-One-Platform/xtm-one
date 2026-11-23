# 🐋 Docker Deployment

OpenCRQ is deployed with Docker through the [XTM Docker bundle](https://github.com/FiligranHQ/xtm-docker),
as OpenCTI and OpenAEV are: it runs OpenCRQ beside XTM One, OpenCTI and OpenAEV, on the bundle's shared
PostgreSQL and object storage, registered with XTM One and connected to OpenCTI and OpenAEV. Follow its
[README](https://github.com/FiligranHQ/xtm-docker#readme), or the [Quick Start Guide](../quick-start.md)
for a first sign-in.

## Attach OpenCRQ to an XTM One that already runs

A bundle deployed before OpenCRQ joined it gets OpenCRQ from the bundle itself: pull its latest
version, add the `OPENCRQ_*` values its `.env.sample` now holds to your `.env` (`OPENCRQ_ADMIN_TOKEN`
and `OPENCRQ_POSTGRES_PASSWORD` among them), and run `docker compose up -d` again. Its
`opencrq-db-init` and `opencrq-bucket-init` services create OpenCRQ's role, database and bucket
beside the existing volumes, and leave them as they are when they already exist. That also moves the
other products to the versions the bundle pins.

The rest of this page is for an OpenCRQ run next to an XTM One the bundle does not start with it: one
on another host, or beside a bundle whose other products should stay on their versions.

### Image

The image is [`filigran/opencrq`](https://hub.docker.com/r/filigran/opencrq) on Docker Hub, built for
`linux/amd64` and `linux/arm64`: a pull takes the host's own.

| Tag         | What it holds                                         |
|:------------|:------------------------------------------------------|
| `latest`    | The highest release.                                  |
| `<version>` | That release. Pin one in production.                  |
| `rolling`   | The tip of `main`, unreleased.                        |

### What it needs

- **A PostgreSQL 17 database** it owns. A role that owns the database is enough, no superuser: the
  application applies its migrations itself at every boot. See [Database](configuration.md#database-postgresql).
- **An S3-compatible bucket**, optional: without the `FILE_STORAGE__*` variables, uploads are off.
  With them, create the bucket beforehand: in production the application does not create it and
  stops at boot while it is missing. See [File Storage](configuration.md#file-storage-rustfs--s3-compatible).
- **XTM One**, reachable from the container, with the same `PLATFORM_REGISTRATION_TOKEN`, a token of
  your own rather than XTM One's published default. When XTM One's containers cannot reach OpenCRQ on
  `XTM_ONE__PLATFORM_URL`, set `OPENCRQ_API_URL` on the XTM One side to an address they can. See
  [XTM One](configuration.md#xtm-one-ai-assistant).
- **HTTPS**: XTM One refuses a sign-in callback on any other scheme than HTTPS, a loopback address
  aside, and browsers keep OpenCRQ's `Secure` session cookie over plain HTTP on `localhost` only, so
  `XTM_ONE__PLATFORM_URL` is the HTTPS address OpenCRQ is published on.

### Variables

| Variable                          | Value                                                                 |
|:----------------------------------|:----------------------------------------------------------------------|
| `HTTP_SERVER__HOST`               | `0.0.0.0`                                                             |
| `HTTP_SERVER__PORT`               | `3002`                                                                |
| `HEALTHCHECK_HTTP_SERVER__PORT`   | `8444`                                                                |
| `POSTGRES__HOST`, `POSTGRES__PORT`, `POSTGRES__USER`, `POSTGRES__PASSWORD`, `POSTGRES__DATABASE` | The database above. |
| `FILE_STORAGE__ENDPOINT`, `FILE_STORAGE__ACCESS_KEY`, `FILE_STORAGE__SECRET_KEY`, `FILE_STORAGE__BUCKET` | The bucket above, if any; `FILE_STORAGE__REGION` too when the store checks it. |
| `XTM_ONE__URL`                    | The address OpenCRQ reaches XTM One on, internal or public.            |
| `XTM_ONE__TOKEN`                  | XTM One's `PLATFORM_REGISTRATION_TOKEN`.                               |
| `XTM_ONE__PLATFORM_URL`           | OpenCRQ's public base URL.                                            |
| `INITIAL_TENANT__ID`, `INITIAL_TENANT__NAME` | The tenant OpenCRQ creates at its first start: see [Initial tenant](configuration.md#initial-tenant). |
| `CONNECTORS__<NAME>__<tenant-uuid>__<FIELD>` | Per tenant: see [Connectors](configuration.md#connectors). |
| `APP__ADMIN__TOKEN`               | Optional: the UUIDv4 a sibling product (OpenAEV, OpenCTI) calls OpenCRQ's API with. See [Admin token as a bearer](configuration.md#admin-token-as-a-bearer). |
| `XTM__OPENCTI_URL`, `XTM__OPENAEV_URL` | Optional: where the product switcher sends a person. See [Application](configuration.md#application-backendfrontend). |

The application refuses to start while one of the three `XTM_ONE__*` is missing. Every other setting
is in the [Configuration Guide](configuration.md).

The initial tenant's id is known before the first start, `INITIAL_TENANT__ID` or its default, so
its connectors (OpenCTI, OpenAEV, Microsoft Graph, Drata, an NVD API key) can be set from the
start. Choose `INITIAL_TENANT__NAME` before that start too: in Community Edition this tenant is the
only one. A tenant created later in Platform › Tenants gets its connector variables afterwards, and
the container restarted to read them.

### On the other products

The bundle also wires the products that call OpenCRQ. Attached by hand, set them yourself:

| Product  | Variable                    | Value                                                     |
|:---------|:----------------------------|:----------------------------------------------------------|
| XTM One  | `OPENCRQ_API_URL`           | Only when its containers cannot reach `XTM_ONE__PLATFORM_URL`: an address of OpenCRQ they can. |
| OpenCTI  | `XTM__OPENCRQ_URL`          | OpenCRQ's public base URL, for its product switcher.      |
| OpenAEV  | `OPENAEV_XTM_OPENCRQ_URL`   | OpenCRQ's public base URL, for its pivots and product switcher. |
| OpenAEV  | `OPENAEV_XTM_OPENCRQ_TOKEN` | OpenCRQ's `APP__ADMIN__TOKEN`, which it calls OpenCRQ's API with. |

### Ports

| Port   | Serves                                                                        |
|:-------|:------------------------------------------------------------------------------|
| `3002` | The application and its API, to publish behind the HTTPS address above.       |
| `8444` | The healthcheck: `200` while every process of the application runs, `503` otherwise, on any path. Keep it off the public network. |

### Example

A Compose service, to adapt to the PostgreSQL, object storage and XTM One already running:

```yaml
services:
  opencrq:
    image: filigran/opencrq:<version>
    restart: always
    environment:
      HTTP_SERVER__HOST: 0.0.0.0
      HTTP_SERVER__PORT: 3002
      HEALTHCHECK_HTTP_SERVER__PORT: 8444
      POSTGRES__HOST: pgsql
      POSTGRES__PORT: 5432
      POSTGRES__USER: opencrq
      POSTGRES__PASSWORD: ${OPENCRQ_POSTGRES_PASSWORD}
      POSTGRES__DATABASE: opencrq
      FILE_STORAGE__ENDPOINT: http://minio:9000
      FILE_STORAGE__ACCESS_KEY: ${OPENCRQ_S3_ACCESS_KEY}
      FILE_STORAGE__SECRET_KEY: ${OPENCRQ_S3_SECRET_KEY}
      FILE_STORAGE__BUCKET: opencrq
      XTM_ONE__URL: http://xtm-one:4000
      XTM_ONE__TOKEN: ${PLATFORM_REGISTRATION_TOKEN}
      XTM_ONE__PLATFORM_URL: https://opencrq.mycompany.com
    ports:
      - "3002:3002"
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:8444/health"]
      interval: 10s
      timeout: 5s
      retries: 30
      start_period: 120s
```
