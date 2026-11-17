# Installation

This page covers the step-by-step deployment of a self-hosted XTM One instance using Docker Compose. Before starting, read the [Overview](overview.md) to understand the deployment models and the architecture.

## Infrastructure requirements

Every deployment runs the same [services](overview.md#architecture): the platform, the workers, PostgreSQL, Redis and object storage, plus the optional components you choose. What changes is how much CPU, RAM, and storage you give them. Start from the baseline below, then adjust with [What drives sizing](#what-drives-sizing).

### Resource baseline

The [official XTM One Helm chart](#kubernetes-and-helm) sets the following per-component resource requests and limits. Use them as a starting point and tune to your observed load:

| Component | CPU request | Memory request | CPU limit | Memory limit | Storage |
|---|---|---|---|---|---|
| Platform | 2 | 4 Gi | 4 | 8 Gi | — |
| Worker | 1 | 2 Gi | 2 | 4 Gi | — |
| PostgreSQL (per instance, 2 instances) | 1 | 2 Gi | 2 | 4 Gi | 64 Gi |
| Redis | 0.5 | 512 Mi | — | 2 Gi | 2 Gi |
| Silo (S3) | 0.5 | 1 Gi | — | 2 Gi | sized to your object volume |

The platform and the worker scale differently. See the [Scaling model](#scaling-model) for how to add worker replicas and keep the database connection budget in range.

**OS**: Any Linux distribution that supports your chosen container runtime. The Docker images are based on `python:3.14-slim` (Debian).

**Disk**: 20 GB minimum for the OS, container images, and initial database. Plan for more based on your knowledge base size and file attachment volume — PostgreSQL data and Silo object storage grow over time.

### What drives sizing

The baseline above suits a typical deployment. Whether you should size up or down depends on how the customer uses the platform. Each resource is driven by a different part of the workload, so profile the use case against these drivers — you rarely need to grow everything at once.

The two dimensions that matter most:

- **Interactive use** — how many people chat with agents at the same time, and how heavy each conversation is (long streams, large tool results). This drives the **platform** and, indirectly, **Redis**.
- **Automation and data** — how many assignments run, how often, and how much content agents ingest and remember. This drives the **worker**, **PostgreSQL**, and **Silo**.

**Platform — CPU / RAM.** Driven by *concurrent interactive users* and live chat streaming (SSE, LLM proxy). Size up when many people use chat at once or you see request latency climb — add CPU/RAM and raise `WEB_WORKERS`. A mostly-automation deployment with few human chat users can stay at the minimum. Chat is stateless per request, so this scales with *concurrency*, not total user count.

!!! tip "In numbers"
    `WEB_WORKERS` is the number of concurrent HTTP/chat processes. The default is **2**; the production rule of thumb is `2 × CPU cores + 1`, so a 4-core platform runs **9** — roughly ×4 the default's request capacity.

**Worker — replicas / CPU.** Driven by *background-job volume and concurrency* — the number of active assignments, how frequently their triggers fire, and how bursty they are (many schedules landing together), plus sub-agent fan-out and knowledge-base ingestion. Size up when runs start queuing or embedding lags. A deployment with a few daily assignments needs one worker; hundreds of assignments, tight schedules, or heavy sub-agent use need replicas.

!!! tip "In numbers"
    Each worker runs up to `MAX_CONCURRENT_RUNS` assignment runs (default **10**) and `MAX_CONCURRENT_SUB_AGENT_TASKS` sub-agent tasks (default **5**) at once, plus up to `WORKER_CONCURRENCY` queued jobs such as document processing (default **20**). Three workers run up to 30 assignment runs at the same time. Throughput scales linearly with replica count, so match the replica count to your peak number of simultaneous runs, and keep the database connection budget in range.

**PostgreSQL — storage / RAM.** Driven by *total data volume* — knowledge-base documents and their vector chunks (RAG), agent memory, and run/audit history retention. Storage grows with knowledge bases and history; RAM helps vector search stay fast as embeddings grow. Size up for large or many knowledge bases, long retention, or slow semantic search.

!!! tip "In numbers"
    Storage is roughly proportional to content — ×2 the ingested documents and history is about ×2 the disk. Start from the chart's 64 Gi baseline and scale from your own corpus size; add RAM only if semantic search slows as embeddings grow.

**Redis — RAM.** Driven by *peak job-queue depth* and *active-session count* — pub/sub for the live activity feed and notifications, plus transient agent state (short/medium-term memory, in-flight chat markers, quota and rate-limit counters). It holds working state, not your data, so it stays small; size up only if you run very high job throughput or many simultaneous active agents. Storage never needs to grow with your knowledge bases.

!!! tip "In numbers"
    The chart baseline is a **512 Mi** request with a **2 Gi** limit — roughly ×4 headroom for bursts. Most deployments never approach the limit because working state is transient; treat a rising limit as a signal of extreme queue depth or session concurrency, not routine growth.

**Silo / S3 — storage.** Driven purely by *stored bytes* — uploaded knowledge-base documents, chat attachments, and agent-generated files (images, code-interpreter charts). CPU/RAM stay negligible; only disk grows. Size the volume to the customer's expected document and attachment footprint, and prefer a managed S3 (via `S3_*`) when that footprint is large or must be highly available.

!!! tip "In numbers"
    Storage equals the sum of stored file sizes — ×2 the uploaded documents and attachments is ×2 the volume. There is no compute multiplier to plan for; provision disk to the customer's raw file footprint plus headroom.

## Container runtime

You need **Docker Engine 24+** with Docker Compose v2.

The repository includes a ready-to-use `docker-compose.yml`.

## Get the deployment files

Check [Releases](https://github.com/XTM-One-Platform/xtm-one/releases) for the latest version tag.

**Option A** — clone a release (builds images from source):

```bash
git clone --branch <version-tag> --depth 1 https://github.com/XTM-One-Platform/xtm-one.git && cd xtm-one
```

**Option B** — use pre-built images (no build required):

```bash
VERSION="<version-tag>"

mkdir xtm-one && cd xtm-one
curl -fSLO "https://raw.githubusercontent.com/XTM-One-Platform/xtm-one/${VERSION}/docker-compose.yml"
curl -fSLO "https://raw.githubusercontent.com/XTM-One-Platform/xtm-one/${VERSION}/.env.sample"
```

Then create an override to pull images instead of building:

```yaml
# docker-compose.override.yml
services:
  platform:
    image: xtmone/platform:<version-tag>
    build: !reset null
  worker:
    image: xtmone/worker:<version-tag>
    build: !reset null
  worker-preview:
    image: xtmone/worker:<version-tag>
    build: !reset null
```

`worker-preview` is a second worker service of the shipped `docker-compose.yml` (two containers). It uses the worker image and runs the same jobs, so it needs the same override.

## Configure environment

```bash
cp .env.sample .env
chmod 600 .env
touch .gitignore
grep -qxF ".env" .gitignore || echo ".env" >> .gitignore
```

Edit `.env` and fill in the required values. At minimum you must set:

- `SECRET_KEY` — generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- `ADMIN_EMAIL` and `ADMIN_PASSWORD`
- `BASE_URL` and `FRONTEND_URL` — the public URL of your instance
- `DB_PASSWORD` — password for the bundled PostgreSQL
- `S3_ACCESS_KEY` and `S3_SECRET_KEY` — credentials for the bundled Silo object storage

If you connect OpenCTI, OpenAEV or OpenCRQ platforms, also set:

- `PLATFORM_REGISTRATION_TOKEN` — the secret those platforms register with (use the same value on each); generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. A standalone deployment that connects none can leave it empty. Left empty, Docker Compose falls back to a published placeholder, which only keeps platforms that are already registered connected - it cannot register a new one - and is reported at startup and in the [security audit](../administration/settings.md#security-audit) (`Settings` → `Security`) until you replace it. While it is in use, the Filigran license certificate is withheld from registration answers, so platforms sub-licensed by your Filigran license cannot prove their Enterprise Edition.

The complete list of available parameters is documented in the [Configuration](configuration.md) section.

## Start

```bash
docker compose up -d
```

The platform waits for PostgreSQL, Redis, and Silo health checks to pass, then automatically runs migrations, creates the admin user, and seeds default templates.

## Verify

```bash
docker compose ps          # all services should show "healthy"
curl localhost:4000/api/health/ready   # 200 = fully ready
```

## Configure the platform

1. Open your `BASE_URL` in a browser.
2. Sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`.
3. Go to `Settings` → `AI Models` — add at least one LLM provider.
4. Configure authentication if needed — see [Authentication](authentication.md).

## Network requirements

### Inbound

| Port | Purpose |
|---|---|
| 4000/tcp | Platform HTTP (chat streams over SSE; XTM One Desktop, runners and relays also connect over WebSocket) |

### Exposing the platform

The platform serves **plain HTTP** on port 4000. In production you put something in front of it that terminates TLS (so users get HTTPS) and forwards traffic to port 4000. Whatever you choose, it must satisfy the same requirements:

| Requirement | Why | Nginx example |
|---|---|---|
| Forward client IP | Needed for rate limiting and audit logs, with `FORWARDED_ALLOW_IPS` naming the proxy ([below](#client-address-and-protocol-behind-the-proxy)) | `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` |
| Forward protocol | OAuth callbacks need the real scheme | `proxy_set_header X-Forwarded-Proto $scheme;` |
| Allow large request bodies | Dataset and evaluation ZIP imports can reach 500 MB, knowledge base uploads 50 MB | `client_max_body_size 500m;` |
| HTTP/2 between browsers and the proxy | A browser opens at most 6 HTTP/1.1 connections per origin. Every tab of the app holds its activity stream, and a chat tab also its conversation's event stream plus, while an answer is written, its message stream: over HTTP/1.1 a few open tabs use them all up and the app stops loading. HTTP/2 carries them on one connection. The proxy may reach the platform over HTTP/1.1 | `listen 443 ssl; http2 on;` (nginx 1.25.1 and later; before, `listen 443 ssl http2;`) |
| Do not buffer streamed responses | Chat answers stream as server-sent events; a proxy that buffers them shows each answer only once it is complete | Nginx honours the `X-Accel-Buffering: no` header the platform sends; on another proxy, turn response buffering off for `/api/` |
| WebSocket upgrade | XTM One Desktop, runners and relays connect over WebSocket; without the upgrade they fall back to slower HTTP long-polling. Chat does not use it | `proxy_set_header Upgrade $http_upgrade;` |
| Generous read/idle timeout | LLM calls and streams can run 60s–5min. An idle stream writes a keep-alive every 15 s, so a read timeout must stay above 15 s | `proxy_read_timeout 300s;` |
| Trust the proxy | The platform honours the forwarded protocol and client IP only from the proxy you name: without it the audit log records the proxy's address for every user, rate limiting sees one client and the A2A endpoints refuse every request (`403 https required`) | On the platform, not the proxy: `FORWARDED_ALLOW_IPS=<proxy address>` ([Client address and protocol behind the proxy](#client-address-and-protocol-behind-the-proxy)) |

Set `BASE_URL` to the public `https://` URL you expose, regardless of which option you pick.

**Option A — Reverse proxy (default).** Best for a standard host with a public IP or load balancer. Terminate TLS with any reverse proxy — Nginx, Caddy, Traefik, or a cloud load balancer — and forward to port 4000. Caddy and Traefik obtain and renew certificates automatically; with Nginx, pair it with your own certificate or a tool such as Certbot.

**Option B — Connection tunnel.** Instead of opening an inbound port, you can front the platform with a tunnel. This fits three cases:

- **Evaluation or demo** — a tunnel such as [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (`cloudflared`) or ngrok gives you a public HTTPS URL in seconds, with no proxy or certificate to configure.
- **No public IP** — for a host behind CGNAT, a home lab, or an edge site, an outbound-only tunnel exposes the platform without any inbound firewall rule.
- **Private-only access** — a mesh VPN such as [Tailscale](https://tailscale.com/) or WireGuard reaches the platform over a private overlay with no public exposure at all.

A tunnel must meet the **same requirements** as a reverse proxy above. Two points need particular attention:

!!! warning "Watch the tunnel timeout"
    XTM One makes long LLM calls and holds long-lived SSE streams for chat. A tunnel with a short origin/idle timeout will cut these off — for example, Cloudflare's proxy enforces a ~100-second origin timeout (HTTP 524). Use a tunnel that allows long-lived connections, and confirm chat streaming works end to end before relying on it.

!!! note "Data residency with public tunnels"
    A **public** tunnel (Cloudflare, ngrok) terminates TLS on that provider's edge, so your traffic transits a third party. If you self-host XTM One specifically for data-residency or air-gapped requirements, prefer a reverse proxy you control or a **private** mesh VPN (Tailscale, WireGuard), which keeps traffic on your own network.

### Client address and protocol behind the proxy

Behind a proxy the platform's peer is the proxy, not the user: the user's address arrives in `X-Forwarded-For` and the original scheme in `X-Forwarded-Proto`. Tell the platform which peer may speak for the client with `FORWARDED_ALLOW_IPS`. Uvicorn, which serves the platform, then rewrites the client address and the scheme from those headers for requests from the listed addresses only, and reads `X-Forwarded-For` from its end, so a client cannot choose its own address even behind a proxy that appends to the header. Without it (uvicorn trusts only `127.0.0.1` by default) every request looks like plain HTTP from the proxy: per-IP rate limiting on `/auth/login` and `/auth/forgot-password` becomes one bucket shared by everyone, the audit trail records the proxy's address, and the A2A endpoints, `/runners/enroll`, `/mcp/oauth/register` and `/mcp/oauth/token` report an unconfirmed transport.

!!! warning "`TRUST_PROXY_HEADERS` trusts any peer"
    `TRUST_PROXY_HEADERS=true` reads the headers from **any** peer and takes the client from the **leftmost** `X-Forwarded-For` value, which the client itself writes behind a proxy that appends to the header, so it suits only a proxy that overwrites the header and is the only thing that reaches the platform. It also takes precedence over `FORWARDED_ALLOW_IPS` when both are set. It is also the **Trust proxy headers** field of Settings > Access and security > Security options (the variable, when set, wins and locks it). While it is on, the platform logs a warning at startup, or when the field is switched on, and the security audit (`/api/v1/security-audit`) reports it. `FORWARDED_ALLOW_IPS` is the recommended setting: switch to it with the steps below.

1. **Find the proxy's address as the platform sees it.** Every request writes an access-log line whose `client_ip` field is the peer; with the default settings it is the proxy's address. Name that address, or the network it lives in.
2. **Make sure the proxy sends both headers**, as in the examples below. `X-Forwarded-Proto` must be one value (`https`): uvicorn ignores a comma-separated list there.
3. **Set `FORWARDED_ALLOW_IPS`** on the platform container and restart: an IP, a CIDR or a comma-separated list. It is read from the container's environment when the platform starts, so it is not a Settings > Access and security field. Hostnames are not resolved and a CIDR needs its host bits at zero (`10.42.0.0/16`); any other entry trusts nobody, and the platform names it in a startup warning. Behind several proxies (a CDN, then a load balancer, then an ingress), list every hop between the user and the platform, since the client is the rightmost address that is not listed. Avoid `*` unless the platform is reachable only through the proxy: it trusts every peer and reads the start of the chain again.
4. **Check it before relying on it**: browse to the platform and read your request's access-log line. Its `client_ip` field is the address the platform uses without `TRUST_PROXY_HEADERS`, and must now show your own address rather than the proxy's (while the flag is on, `forwarded_for` shows what the flag reads). If it still shows the proxy, the list does not name it: correct it and restart.
5. **Turn `TRUST_PROXY_HEADERS` off where it is set**: remove `TRUST_PROXY_HEADERS` (or set it to `false`) and restart the platform, or, when the variable is not set, turn off **Trust proxy headers** in Settings > Access and security > Security options.
6. **Check the result**: the startup log no longer carries the warning about the flag, the security audit no longer reports it, and the access log's `forwarded_for` field, written only under `TRUST_PROXY_HEADERS`, disappears.

=== "Nginx"

    ```nginx
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    ```

    Appending with `$proxy_add_x_forwarded_for` is safe here: the platform reads the chain from its end. For nginx on the Docker host in front of the Compose stack, the platform sees the Docker bridge gateway of the Compose network (for example `172.18.0.1`, `docker network inspect <project>_default`); for nginx in the same Compose network, its container address or the network's subnet:

    ```bash
    FORWARDED_ALLOW_IPS=172.18.0.1
    ```

=== "Traefik"

    Traefik sends both headers by default and drops the `X-Forwarded-*` values a client sent unless the entry point lists the sender in `forwardedHeaders.trustedIPs` (list a load balancer or CDN in front of Traefik there, not in the platform). Name Traefik itself, by its address or its Docker network's subnet:

    ```bash
    FORWARDED_ALLOW_IPS=172.20.0.0/16
    ```

=== "Kubernetes ingress"

    The peer is the ingress controller's pod, so name the cluster's pod network (for example `10.42.0.0/16` on k3s, `10.244.0.0/16` on a kubeadm cluster with Flannel) on the platform container:

    ```yaml
    env:
      - name: FORWARDED_ALLOW_IPS
        value: "10.42.0.0/16"
    ```

    Neither Helm chart sets it yet: until they gain a first-class value, set it in the platform's `env` map, `xtmOne.platform.env` in `helm-xtm-one` and `xtmone.platform.env` in `helm-xtm-suite`:

    ```yaml
    xtmOne:
      platform:
        env:
          FORWARDED_ALLOW_IPS: "10.42.0.0/16"
    ```

    For an ingress controller on the host network, name the node addresses instead. ingress-nginx writes into `X-Forwarded-For` the address it received the connection from; behind a cloud load balancer that is the load balancer's unless the service keeps the client address (`externalTrafficPolicy: Local`, PROXY protocol) or the controller trusts the load balancer's header (`use-forwarded-headers` with `proxy-real-ip-cidr`).

#### When the platform reports an undeclared proxy

The platform reads the client address from `X-Forwarded-For` only, never from `X-Real-IP` or `Forwarded`, and only from a peer `FORWARDED_ALLOW_IPS` names. When an audited request - a sign-in, a change an administrator makes - carries `X-Forwarded-For` from a peer it does not name, whether no proxy is declared or another one is, or names its client only in `Forwarded` or `X-Real-IP`, the platform logs one warning naming that peer and what to do, once a day for all its processes and replicas, and again after a restart with another `FORWARDED_ALLOW_IPS`:

```text
A request carried X-Forwarded-For while no reverse proxy is declared - "Trust proxy headers" (TRUST_PROXY_HEADERS) is off and FORWARDED_ALLOW_IPS names none beyond this host - so the audit trail recorded the address of the peer that relayed it, 10.42.0.17, instead of the user's. If that peer is your reverse proxy or ingress, set FORWARDED_ALLOW_IPS to its address or CIDR (behind a Kubernetes ingress, the cluster's pod network) and restart. Said once a day for the whole platform; the security audit (Settings > Access and security) reports it too.
```

The [security audit](../administration/settings.md#security-audit) (`Settings` → `Security`) reports it as the warning "The audit trail records a proxy's address, not users'", naming the latest such peer any replica saw in the last day, with the exact remedy. A proxy declared under another address than the one it connects from - the Docker host's LAN address for a proxy on that host, which reaches the container from the Compose network's gateway - is reported the same way: name the address the warning gives. With no reverse proxy in front of the platform, such a request carried headers a client sent itself: nothing needs to change, the platform ignores them.

Once the platform has restarted with the proxy named, sign out and sign in again, then open the **Audit Logs** page: your login shows your own address, not the proxy's. The warning is no longer logged, and the security audit stops reporting the proxy - a proxy it reported for sending no `X-Forwarded-For` drops out 24 hours after its last such request.

### Serving under a path prefix

By default the platform expects to be reached at the root of its host (`https://xtm.example.com/`). To serve it under a path prefix instead — one host shared by several products, such as `https://intranet.example.com/xtm-one/` — set `BASE_PATH` to the prefix and include it in `BASE_URL` and `FRONTEND_URL`:

```bash
BASE_PATH=/xtm-one
BASE_URL=https://intranet.example.com/xtm-one
FRONTEND_URL=https://intranet.example.com/xtm-one
```

The platform then serves its interface, API, WebSockets and MCP endpoints under `/xtm-one/...`, and every absolute link it hands out (OAuth callbacks, webhook URLs, SSO redirects, the embeddable chat widget, e-mails) carries the prefix. Both reverse-proxy styles work — the platform recognises a request whether the proxy forwarded the prefix or stripped it — so use whichever your proxy makes natural:

=== "Nginx (prefix forwarded)"

    ```nginx
    location /xtm-one/ {
        proxy_pass http://127.0.0.1:4000;   # no trailing slash: the prefix is forwarded as is
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
        client_max_body_size 500m;
    }
    location = /xtm-one { return 301 /xtm-one/; }
    ```

=== "Nginx (prefix stripped)"

    ```nginx
    location /xtm-one/ {
        proxy_pass http://127.0.0.1:4000/;  # trailing slash: nginx strips /xtm-one before forwarding
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Prefix /xtm-one;  # tells the platform the prefix was stripped
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
        client_max_body_size 500m;
    }
    location = /xtm-one { return 301 /xtm-one/; }
    ```

=== "Traefik"

    ```yaml
    http:
      routers:
        xtm-one:
          rule: "Host(`intranet.example.com`) && PathPrefix(`/xtm-one`)"
          service: xtm-one
          # Optional - the platform accepts the prefix either way:
          # middlewares: [xtm-one-strip]
      services:
        xtm-one:
          loadBalancer:
            servers:
              - url: "http://xtm-one-platform:4000"
      middlewares:
        xtm-one-strip:
          stripPrefix:
            prefixes: ["/xtm-one"]
    ```

Three constraints:

- `BASE_URL` and `FRONTEND_URL` must carry the prefix. The platform routes requests by `BASE_PATH`, but it builds outgoing links from `BASE_URL` (OAuth callbacks, webhooks, the MCP resource identifiers) and sends the browser back to `FRONTEND_URL` after an SSO or OAuth round-trip; when either disagrees it logs a warning at startup and every such link lands on the proxy's 404.
- The prefix must not start with a namespace the platform serves itself — `/api`, `/v1`, `/mcp`, `/xtm`, `/a2a`, `/office`, `/metrics`, `/assets`, `/.well-known` — nor with the name of one of its static files (`/favicon.ico`, `/logos`, `/robots.txt`, `/site.webmanifest`, ...), because a stripped request under such a prefix could not be told apart from a direct request for that resource. Such a value is refused at startup.
- The prefix is a plain path: ASCII letters, digits and `-`, `_`, `.`, `~` between slashes, no percent-encoding, no query string. `/xtm-one` and `/apps/xtm-one` are fine (`xtm-one/` and `/xtm-one/` are read as `/xtm-one`); `/xtm%20one` or a prefix with an accented letter is refused.
- A prefix that is also the address of a platform page (`/tools`, `/agents`, `/chat`, `/settings`, `/login`, ...) is refused until you declare the proxy with `BASE_PATH_MODE`. The platform tells the two proxy styles apart per request - an `X-Forwarded-Prefix` header naming the prefix means the proxy stripped it, otherwise a path under the prefix was forwarded - and for such a prefix the second reading is ambiguous: behind a stripping proxy the public `/tools/tools/123` arrives as `/tools/123`, which is also the forwarded form of another page. `BASE_PATH_MODE=forwarded` declares that the proxy keeps the prefix (nginx `proxy_pass` without a trailing slash, Traefik without `StripPrefix`); `BASE_PATH_MODE=stripped` declares that it removes it and sends `X-Forwarded-Prefix` - Traefik's `StripPrefix` does so by itself, nginx needs the `proxy_set_header X-Forwarded-Prefix` line of the example above. Any other prefix needs no declaration.

`BASE_PATH` is the one source of the prefix: `X-Forwarded-Prefix` is read only as the per-request signal that the prefix it names was stripped (a header naming anything else is ignored), and uvicorn's `--root-path` is not needed (a deployment that already sets it to the same value keeps working). A request for the bare prefix (`https://intranet.example.com/xtm-one`, no trailing slash) is answered with a redirect to `/xtm-one/` when the proxy forwards it, and served directly when the proxy strips it; that redirect is built from the scheme the platform sees, so behind a TLS-terminating proxy declare it with `FORWARDED_ALLOW_IPS` (as for any HTTPS deployment; an nginx reaching a Docker Compose deployment at `127.0.0.1:4000`, as in the examples above, connects from the Compose network's gateway - see [Client address and protocol behind the proxy](#client-address-and-protocol-behind-the-proxy)) - or keep the `location = /xtm-one` rule of the examples, which lets the proxy answer it. With Docker Compose, `BASE_PATH`, `BASE_PATH_MODE`, `BASE_URL` and `FRONTEND_URL` set in `.env` reach both the platform and the worker containers (the worker builds absolute links too). The same values apply to the Helm chart's environment (`BASE_PATH` / `BASE_PATH_MODE` on the platform and worker deployments, through the chart's extra env until it gains a first-class value) and to the ingress path; `PRESENTATION_RENDER_BASE_URL`, when set, is an origin and the platform appends the prefix to it itself (a value that already carries a path is used as is). The container itself still answers `/api/health`, `/api/health/ready` and `/metrics` on its own port without the prefix, so liveness / readiness probes and Prometheus scrapes are unchanged.

#### MCP clients authenticating with OAuth

An MCP client (Claude, Cursor, ...) that signs in to this platform's MCP endpoints with OAuth discovers the authorization server from the issuer `https://intranet.example.com/xtm-one`. RFC 8414 and RFC 9728 put the well-known segment *before* the issuer's path, so the client asks the **root of the host** — `https://intranet.example.com/.well-known/oauth-authorization-server/xtm-one` and `/.well-known/oauth-protected-resource/xtm-one/mcp/user` — which the `/xtm-one/` rule above never sees. The platform serves both forms (the path-inserted one and the `/xtm-one/.well-known/...` one); forward the well-known paths to it as well, without stripping anything:

=== "Nginx"

    ```nginx
    location /.well-known/oauth-authorization-server/ { proxy_pass http://127.0.0.1:4000; }
    location /.well-known/oauth-protected-resource/  { proxy_pass http://127.0.0.1:4000; }
    ```

=== "Traefik"

    ```yaml
    http:
      routers:
        xtm-one-oauth-discovery:
          rule: "Host(`intranet.example.com`) && PathPrefix(`/.well-known/oauth-`)"
          service: xtm-one   # no StripPrefix on this router
    ```

Clients authenticating with an API key (`fcp-...`) and the platform's own connections to other MCP servers need nothing of this.

!!! note "Not served under a prefix in this release"
    The Microsoft [Office add-in](../usage/office-add-in.md) (`/office/`) is built for the root of the host: its bundle references `/office/assets/...` and it recognises the platform by that path. Under `BASE_PATH` the add-in's page is served at `/xtm-one/office/` but the add-in itself does not load there; deploy it from a platform served at the root, or wait for the follow-up.

### Internal (between containers)

| From | To | Port |
|---|---|---|
| Platform, Worker | PostgreSQL | 5432 |
| Platform, Worker | Redis | 6379 |
| Platform, Worker | Silo/S3 | 9000 |
| Platform, Worker | OpenSandbox (optional) | 8090 |

### Air-gapped deployments

Air-gapped deployments work if you use a local AI provider (Ollama or any OpenAI-compatible endpoint on your network) and the built-in Silo for storage. To get Docker images into an air-gapped network:

```bash
# On a machine with internet access — export the images (pre-built images setup from Option B):
docker compose pull
docker save xtmone/platform:<version-tag> xtmone/worker:<version-tag> pgvector/pgvector:pg17 redis:8-alpine pgsty/silo:latest > xtm-one-images.tar
# With the optional OpenSandbox profile, add its server and sandbox images (see Configuration > OpenSandbox).

# Transfer xtm-one-images.tar to the air-gapped host, then load:
docker load < xtm-one-images.tar
```

## Scaling model

[What drives sizing](#what-drives-sizing) tells you *which* resource to grow for a given use case. This section covers the *mechanics* — the structural reason the platform and the worker scale in fundamentally different ways, and the one constraint that couples them.

!!! info "Two different \"workers\""
    The word *worker* means two separate things here. The **worker service** (the `worker` container / Helm `worker` replicas) is the background-job runner you scale horizontally. `WEB_WORKERS` is unrelated: it sets the number of **uvicorn HTTP worker processes inside the platform container**, which serve the API and chat. Adding worker replicas does not change `WEB_WORKERS`, and vice versa.

- **The platform scales vertically first, and can run more than one replica.** Give it more CPU/RAM and raise `WEB_WORKERS` for more throughput. Extra platform replicas are supported for availability (a rollout or a node drain never leaves zero pods serving) and for load: every background service it runs is coordinated across processes, because a single pod already runs `WEB_WORKERS` processes (2 by default) that each run all of them. See [Running more than one platform replica](#running-more-than-one-platform-replica) for what is shared, and the few things that stay per process.
- **The worker service scales horizontally** — it holds no singleton state, so you add replicas for more background-job throughput. Scaling is manual (there is no autoscaler). The shipped `docker-compose.yml` starts three worker containers: `worker` and the two `worker-preview` containers. See [What drives sizing](#what-drives-sizing) for *when* to add more.
- **The connection budget couples the two** — every platform HTTP worker process and every worker replica opens its own PostgreSQL pool, so `max_connections` must accommodate `(DB_POOL_SIZE + DB_MAX_OVERFLOW) × (WEB_WORKERS × platform replicas + worker replicas) + overhead`. Count the rollout too: with a surge rollout (`maxSurge: 1`) one extra platform pod runs next to the others until the old one is gone. Recalculate it whenever you change a count or the pool sizes (see [Configuration](configuration.md#performance)).

### Running more than one platform replica

Each platform process (every uvicorn worker of every replica) runs the same startup and background work. What makes that safe:

| What | How it is coordinated |
|---|---|
| Database migrations and built-in data seeding | PostgreSQL advisory locks: one process migrates / seeds at a time, the others wait and re-run the idempotent steps |
| Vector index builds | PostgreSQL advisory lock |
| Assignment scheduler (cron triggers, integration event polling, sweeps, maintenance) | Redis window lock: one tick per minute across all processes and replicas |
| Anonymous usage telemetry | Redis window lease: one process exports per interval for the whole install, with values read from the database; a process that dies hands over when the lease expires, and a Redis outage skips the export rather than sending one per process |
| A2A stale-task reaper, A2A partner card sweep | Rows claimed with `FOR UPDATE SKIP LOCKED`: concurrent sweeps take disjoint work |
| Chat streams | Turn state and frames through Redis: a stream on any replica follows a turn running on another, and Stop, steering and approvals reach the turn wherever it runs |
| Runner connections (XTM One Desktop / headless runners) | Redis pub/sub wakes the replica holding a runner's socket; results are awaited through Redis on any replica |
| Activity stream (SSE), presence, rate limits, OAuth / MCP OAuth state, desktop sign-in | Redis |
| Discord bot | Each process opens its own gateway session; the first to claim a message (Redis) answers it |
| Platform branding (name, logo) | Each process, the worker service included, re-reads it every minute: an admin change reaches every process within a minute |
| Channel webhooks (Slack, Teams, Telegram, WhatsApp, Google Chat), REST and MCP endpoints | Stateless per request; Slack events are also de-duplicated in Redis |

What stays per process, and what it means:

- **Browser tool sessions.** The headless browser an agent drives lives in the process that ran the turn. A later turn served by another process opens a fresh browser (the page and its cookies are gone). Each chat message is its own request, so two turns of a conversation can run in different processes; enable session affinity on the ingress if agents rely on a browser session across turns.
- **Outbound rate limiters for integrations** (for example the HubSpot search pacer) are per process: with more processes the combined request rate against a provider can exceed what one process allows itself. Integrations retry on `429`, so the effect is extra retries, not failures.
- **Prometheus metrics** (`/metrics`) are per process: a scrape is answered by whichever `WEB_WORKERS` process takes it, so apart from the gauges recomputed on every scrape (quota, assignment queue, relays, sandbox; see [Monitoring](monitoring.md#metrics)) a series is a sample of one process, not a counter to read with `rate()` or `increase()`. The logs are the exact record.
- **Caches** of licence state, AI settings and LLM provider clients are per process with short expiries (seconds to a few minutes): after a change, other processes follow within that time.

Migrations run on the first new pod to start, while the previous version still serves: a rolling update (with any replica count) relies on every migration staying compatible with the version before it, which the project's migration rules require.

**Running more workers (Docker Compose).** The `worker` service has no fixed container name, so you can scale it directly:

```bash
docker compose up -d --scale worker=3
```

Each worker creates its own connection pools, so recalculate the PostgreSQL connection budget accordingly. On Kubernetes, add replicas through the Helm chart instead.

## Kubernetes and Helm

XTM One can be deployed two ways: the Docker Compose stack described above, which runs the full stack on a single host, or the **official Helm chart**, which runs the same container images on Kubernetes. Choose the one that fits your infrastructure.

The chart deploys the `platform` and `worker` images and can optionally provision the backing services. Its prerequisites are:

- Kubernetes 1.28+ and Helm 3.12+
- the [CloudNativePG](https://cloudnative-pg.io/) operator for managed PostgreSQL with pgvector
- an S3-compatible object store: the chart ships [Silo](https://github.com/pgsty/silo), the community-maintained fork of MinIO (or point the platform at an external S3)
- Redis (deployed separately, or an external Redis)

Wire the [health probes](monitoring.md#health-probes) as `livenessProbe` (`/api/health`) and `readinessProbe` (`/api/health/ready`). The worker scales horizontally by adding replicas; the platform scales vertically first and can run more than one replica (see the [Scaling model](#scaling-model) above).

Give the platform pod a `terminationGracePeriodSeconds` above `GRACEFUL_TIMEOUT` plus 15 seconds (`180` with the default `GRACEFUL_TIMEOUT` of 120): a stopping platform pod (a rolling update, a scale-in, a node drain) lets its running chat turns finish for `GRACEFUL_TIMEOUT`, then the ones still running store a reply saying they were interrupted and what they had done. Kubernetes' own default of 30 seconds kills the pod before either happens, and those turns end with no reply. A rolling update with `maxUnavailable: 0` and `maxSurge: 1` keeps the old pod serving until the new one is ready.

!!! note "Under construction"
    The Helm chart deployment guide is under construction. The chart location, its release status, the full `values.yaml` reference, and a validated production topology will be documented here soon.

## Next step

Configure the platform parameters in the [Configuration](configuration.md) section, and read the [Upgrade](upgrade.md) procedure before your first version bump.
