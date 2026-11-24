# Configuration

This page documents all available environment variables for XTM One. The [`.env.sample`](https://github.com/XTM-One-Platform/xtm-one/blob/main/.env.sample) file is the canonical source and includes inline comments for each variable.

!!! note "Docker Compose"
    Docker Compose reads `.env` only to fill in `docker-compose.yml`: a variable reaches the platform and worker containers only when `docker-compose.yml` lists it. It lists every on/off switch of `.env.sample` (`true` / `false`) except `DEBUG`, `A2A_ALLOW_INSECURE_LOOPBACK` and `TRUST_PROXY_HEADERS`; when `.env` does not set one, the container gets the platform's own default, or a blank value for a switch that is also in **Settings > Access and security**, so the field stays editable there. A blank on/off switch counts as unset. To pass another variable, add it to the `environment` of the `platform` and worker services (or to a `docker-compose.override.yml`).

!!! warning "SECRET_KEY"
    This is the most critical value. It encrypts all stored integration credentials. If you lose it or change it, all encrypted data becomes unrecoverable.

!!! warning "ADMIN_PASSWORD"
    The platform resets the admin password to the value in `.env` on every startup. This means `.env` is a standing password reset mechanism — anyone with read access to the file can take over the admin account by restarting the service. Treat `.env` as a privileged secret (owner-only permissions, never committed to version control). After first login, consider rotating the password via the UI and keeping the `.env` value in sync.

## Security

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | **Yes** | — | JWT signing + Fernet encryption key. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Never change after deployment. |
| `ADMIN_EMAIL` | No | `admin@filigran.io` | Default admin account email. Created or updated on every startup: the account is also made an administrator and re-enabled each time, so it stays a way back into the platform. |
| `ADMIN_PASSWORD` | **Yes** | — | Default admin account password. Reset to this value on every startup. At most 72 bytes (what bcrypt stores); a longer value is refused at startup. |
| `ADMIN_TOKEN` | No | — | Programmatic API token for the admin (must start with `fcp-`, min 32 chars). Like every bcrypt-hashed key, only its first 72 bytes are checked. |
| `FORWARDED_ALLOW_IPS` | No | `127.0.0.1` | Proxy addresses allowed to set `X-Forwarded-Proto` / `X-Forwarded-For`, as an IP, CIDR or comma-separated list. Set this behind a TLS-terminating proxy: without it the platform sees every request as plain HTTP from the proxy's address, which records the proxy's address in the audit log for every user, makes the A2A endpoints refuse every request (`403 https required`), makes `/runners/enroll`, `/mcp/oauth/register` and `/mcp/oauth/token` log an unconfirmed-transport warning (they refuse too only under `CREDENTIAL_TRANSPORT_STRICT`), and collapses per-IP rate limiting into one shared bucket. Name the proxy as the platform container sees it (see [Client address and protocol behind the proxy](installation.md#client-address-and-protocol-behind-the-proxy)). Hostnames are not resolved and a CIDR needs its host bits at zero (`10.42.0.0/16`, not `10.42.0.1/16`): uvicorn keeps any other entry as a literal that matches no peer, and the platform names such entries in a startup warning. uvicorn reads it from the platform container's environment when the platform starts, so it is not a Settings > Access and security field and a change needs a restart; the shipped `docker-compose.yml` passes it to the platform container. When audited requests come through a peer it does not name, the platform logs a warning naming that peer and the security audit reports it (see [Client addresses in the audit trail](#client-addresses-in-the-audit-trail)). Prefer it over `TRUST_PROXY_HEADERS`; avoid `*`, which lets any caller choose its own `scope["client"]`; it means "every peer" only as the whole value. |
| `TRUST_PROXY_HEADERS` | No | `false` | When `true`, the platform takes the client address from the leftmost `X-Forwarded-For` value and the scheme from `X-Forwarded-Proto`, from any peer and ahead of `FORWARDED_ALLOW_IPS`. Behind a proxy that appends to `X-Forwarded-For` that value is whatever the client sent, so a caller can choose its rate-limit bucket and the address the audit trail records: enable it only behind a proxy that overwrites the header and is the only thing that reaches the platform, and prefer `FORWARDED_ALLOW_IPS`. While it is on the platform logs a warning at startup (or when it is switched on in Settings > Access and security) and the security audit warns that forwarded headers are trusted from any peer. Switching to `FORWARDED_ALLOW_IPS`: [UPGRADING.md](https://github.com/XTM-One-Platform/xtm-one/blob/main/UPGRADING.md) and [Client address and protocol behind the proxy](installation.md#client-address-and-protocol-behind-the-proxy). The shipped `docker-compose.yml` does not pass the variable to the platform container. Also in **Settings > Access and security** (**Trust proxy headers**); the variable, when set, wins and locks the field. |
| `RATE_LIMIT_IPV6_PREFIX` | No | `128` | Prefix length the per-IP rate limits (login, forgot and reset password, invitation registration, MFA verification, A2A pre-authentication, MCP OAuth registration, authorization and token, the public chat widget, Content Security Policy violation reports) key a native IPv6 client on. `128` keeps one bucket per address. `64` is opt-in: it stops a client drawing a fresh bucket from every address of the /64 it is handed, but every device of one /64 - a corporate IPv6 LAN, for example - then shares one budget. From `32` to `128`; any other value stops the platform at startup. The key in use is logged once at startup, and the security audit reports an info finding recommending `64` while the value is above `64`. IPv4 clients (IPv4-mapped included) are always keyed per address, and the audit trail, the access log and the webhook diagnostics always record the full address. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `CLIENT_IP_STRICT` | No | `false` | Opt-in: key, log and store every client address (the `X-Forwarded-For` hop under `TRUST_PROXY_HEADERS`, the peer otherwise) in its canonical form - port, brackets and IPv6 zone index dropped, an IPv4-mapped address read as IPv4, IPv6 lowercase and compressed - so one client is one rate-limit key whatever the proxy wrote; behind a proxy that writes `IP:port` under `TRUST_PROXY_HEADERS`, every connection is otherwise its own bucket. `false` uses an address as it was read, as in earlier releases. Either way a value that is not an IP address is ignored, with a warning logged once, and an address longer than the audit trail's 45 characters is stored in canonical form. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `CREDENTIAL_TRANSPORT_STRICT` | No | `false` | Opt-in. By default `/runners/enroll`, `/mcp/oauth/register` and `/mcp/oauth/token` serve a request whose transport the platform cannot confirm encrypted, with the answers of earlier releases, and log it once per route (`... served over unconfirmed transport ...`). `true` refuses such a request the way the A2A endpoints do, before reading it or issuing anything: `/runners/enroll` answers `403 {"detail": "https required"}`, `/mcp/oauth/register` answers `400 {"error": "invalid_client_metadata", "error_description": "https required"}` (RFC 7591), `/mcp/oauth/token` answers `400 {"error": "invalid_request", "error_description": "https required"}`. Real loopback stays exempt under `DEBUG` or `A2A_ALLOW_INSECURE_LOOPBACK`, and so do the end-to-end suite's `TEST_ONLY_INSECURE_TRANSPORT_PEERS`. Behind a TLS-terminating proxy, set `FORWARDED_ALLOW_IPS` first and check the warning no longer appears. The security audit reports `network.credential_transport_strict_off` while it is off - a warning while the platform is served over plain HTTP (`BASE_URL` not https, `DEBUG` off) and an active runner or a registered MCP OAuth client holds credentials those routes issued, information otherwise - and `network.credential_transport_strict_on` (pass) when it is on. Also in **Settings > Access and security** (**Require HTTPS for runner enrollment and MCP tokens**); the variable, when set, wins and locks the field. |
| `TEST_ONLY_INSECURE_TRANSPORT_PEERS` | No | — (empty) | **End-to-end tests only; never set it on a deployment.** Peer IPs or CIDRs, comma-separated, whose plain HTTP requests count as encrypted on the A2A surface, for a test suite that reaches a containerised platform from the Docker bridge gateway (the A2A e2e job sets `172.16.0.0/12,192.168.0.0/16`). It applies to the real socket peer only: a request carrying `X-Forwarded-For` is never exempt. The platform refuses to start with it unless the variable `A2A_ALLOW_INSECURE_LOOPBACK=true` is set as well (the end-to-end stack's loopback exemption, which this extends; its Settings > Access and security field does not count, since the check runs at startup), or with an entry that is malformed or not a private network; while set, it is logged as a warning at every boot. Behind a TLS-terminating proxy, set `FORWARDED_ALLOW_IPS` instead. |
| `A2A_ALLOW_INSECURE_LOOPBACK` | No | `false` | **Testing only.** Count a plain HTTP request from real loopback (`127.0.0.1`, `::1`) as encrypted, so the A2A endpoints accept it: what lets a deployment federated with itself reach its own agent card without terminating TLS. Only the real socket peer counts: a request carrying `X-Forwarded-For` is never exempt. Never set it behind a reverse proxy on the same host, where every request arrives from loopback. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `A2A_CLIENT_DEV_TRUSTED_ORIGINS` | No | — | **Local development only.** Comma-separated origins (`scheme://host[:port]`) that outbound A2A requests may reach over plain HTTP and at addresses the destination guard otherwise refuses (loopback, private networks), to exercise the outbound half against a second deployment on the same machine. Every other destination stays refused, a listed origin is still pinned, and cloud metadata addresses stay refused even when listed. A malformed entry stops the platform at startup, and a configured list is logged at `WARNING` on every boot (and when it is saved in Settings > Access and security). Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `OAUTH_TOKEN_URL_STRICT` | No | `false` | Opt-in strict checks on OAuth token URLs. Every OAuth token request (authorization-code exchange and token refresh, for integrations and MCP servers) checks its token URL, both when the URL is saved and before each request, and every address its host resolves to. By default only a cloud-metadata address (`169.254.169.254` and the other instance-metadata endpoints) and a URL no request can be sent to (no host, an unreadable URL, port or host, a scheme other than `http`, `https`, `ws` or `wss`) are refused, so every token URL that worked before keeps working, loopback included. Set to `true` to also refuse token URLs on loopback, link-local, unspecified, reserved and multicast addresses, and any scheme other than `http` or `https`; private networks stay allowed, so an on-prem identity provider keeps working. A refusal names the setting that lifts it. Before turning it on, point every token URL on loopback at another address of the identity provider (a private-network address or a Docker service name such as `http://keycloak:8080`), or list its origin in `OAUTH_TOKEN_DEV_TRUSTED_ORIGINS` on a development machine. **Upgrading:** no action required; the strict mode is opt-in. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `OAUTH_TOKEN_DEV_TRUSTED_ORIGINS` | No | — | **Local development only.** With the strict checks on (`OAUTH_TOKEN_URL_STRICT`), comma-separated origins (`scheme://host[:port]`) whose OAuth token URL may resolve to a loopback address. Listing an origin lifts the loopback refusal for that origin only; link-local and metadata addresses stay refused. No effect while the strict checks are off, which accept loopback token URLs anyway. A malformed entry stops the platform at startup, and a configured list is logged at `WARNING` on every boot (and when it is saved in Settings > Access and security), or at `INFO` as having no effect when `OAUTH_TOKEN_URL_STRICT=false` is set. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `MCP_SERVER_URL_POLICY` | No | `permissive` | Which addresses a remote MCP server (`sse`, `streamable_http`) may be reached at. Checked when its URL is saved (the form, the agent builder, every import: a `422`) and on the address every connection to it actually opens - the connectivity test, tool calls, the authorization probe and OAuth discovery and registration included - with no second DNS lookup, each redirect hop checked again. `permissive` refuses only cloud metadata addresses (`169.254.169.254` and the other instance-metadata endpoints), so every MCP server that worked before keeps working, one on `localhost` included. `private_networks` also refuses loopback, link-local, unspecified, reserved and multicast addresses; private networks stay allowed, so an on-prem MCP server keeps working. `public_only` refuses every address that is not globally routable: reach an on-prem server through a [relay group](../usage/resources/relays.md) instead, whose relay checks its own allow-list. A cloud metadata address is refused in every mode, and a URL must be `http` or `https` with a host. A refusal answers one generic message (the address and its class go to the backend log only). A server routed through a relay group is judged only on what its URL spells. Behind an outbound proxy (`HTTP_PROXY` / `HTTPS_PROXY`, `NO_PROXY` applied), `permissive` keeps sending through it after refusing a URL that is, or resolves from the platform to, a metadata address; the stricter modes never use the proxy and connect directly, since the address connected to is what they judge. A server already saved at a refused address stops connecting until its URL changes or its origin is listed in `MCP_SERVER_TRUSTED_ORIGINS`; saving it without changing its URL, transport or relay group still works. A blank value is unset (`docker-compose.yml` forwards it to the platform and the worker). **Upgrading:** no action required; the stricter modes are opt-in. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `MCP_SERVER_TRUSTED_ORIGINS` | No | — | Under `MCP_SERVER_URL_POLICY=private_networks` or `public_only`, comma-separated origins (`scheme://host[:port]`) a remote MCP server may be reached at whatever address they resolve to - an internal MCP server you vouch for. Matched on the origin: `http://mcp.internal:8080` covers every path on that service and nothing on another port, scheme or host. Cloud metadata addresses stay refused for a listed origin. No effect under `permissive`, which accepts those addresses anyway. A malformed entry stops the platform at startup. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `CONTENT_SECURITY_POLICY_MODE` | No | `report-only` | Content-Security-Policy on every HTML page the platform serves. `report-only` blocks nothing: browsers report what the policy would have blocked to `/api/v1/csp-report`, where the reports are counted and logged; the security audit lists the violations of the last 7 days. `enforce` sends the same policy as `Content-Security-Policy`, so browsers block what it does not allow. `off` sends no policy. Any other value fails startup. See [Security headers](security-headers.md). |
| `CONTENT_SECURITY_POLICY_FRAME_ANCESTORS` | No | — | Origins, besides the platform itself and the platforms registered with it (only when `PLATFORM_REGISTRATION_TOKEN` is a value of your own, and at most 20 of them), allowed to show the platform's pages in an iframe once the policy is enforced (an intranet portal, for instance). Comma-separated CSP sources: `https://portal.example.com`, `https://*.example.com`. A malformed entry fails startup; one that admits every site (`*`, `https:`, `https://*`) is accepted but logged as a warning at startup. See [Security headers](security-headers.md#who-may-frame-the-platform). |
| `SECRET_VAULT_<NAME>_TYPE` | No | — | Declares a secret vault named `<name>` (`hashicorp`, `aws`, `gcp`, `azure` or `onepassword`) in the environment, for a deployment managed as code. Vaults are otherwise connected and granted in Settings > Access and security > Secret vaults, with no variable and no restart. Its other settings are `SECRET_VAULT_<NAME>_<SETTING>` (`ADDRESS`, `AUTH_METHOD`, `TOKEN_FILE`, `REGION`, `WRITERS`, `PATHS`, ...). Read once at startup, on the platform and the worker; listed read-only in Settings. See [Secret vaults](secret-vaults.md#advanced-declare-a-vault-in-the-environment). |
| `SECRET_VAULT_CACHE_SECONDS` | No | `300` | How long a value read from a vault is kept in memory, per process, for a vault with no cache duration of its own: a secret rotated in the vault is used once this time has passed, or at once when an integration's service refuses the cached value. `0` reads the vault on every use. See [Secret vaults](secret-vaults.md#cache-and-rotation). |
| `SECRET_VAULT_TRUSTED_ORIGINS` | No | — | Comma-separated origins (`scheme://host[:port]`) that a vault connected in Settings > Access and security > Secret vaults may reach on a loopback address: an on-host vault. Such a vault's address is otherwise refused on loopback, link-local, reserved and cloud metadata addresses, checked when it is saved, tested and on every connection; private networks are allowed. Link-local and metadata addresses stay refused for a listed origin. Environment only, never a Settings field. A malformed entry stops the platform at startup. See [Secret vaults](secret-vaults.md#network-and-tls). |

### Client addresses in the audit trail

The audit trail records the address each action came from, and reads it through a declared proxy only: `FORWARDED_ALLOW_IPS` naming the proxy (uvicorn then replaces the proxy's address with the client address from `X-Forwarded-For` before the platform reads it), or **Trust proxy headers** in [Settings > Access and security](#settings-security-options) (`TRUST_PROXY_HEADERS`), which believes the header from any peer. The client address is read from `X-Forwarded-For` only, never from `X-Real-IP` or `Forwarded`. Behind a reverse proxy or an ingress declared by neither, it records the proxy's address, because any client can write that header itself. When an audited request carries `X-Forwarded-For` from a peer `FORWARDED_ALLOW_IPS` does not name - no proxy declared at all (Trust proxy headers off, `FORWARDED_ALLOW_IPS` unset or naming nothing beyond `127.0.0.1`), or another proxy declared than the one the request came through, such as a proxy on the Docker host that reaches the container from the Compose network's gateway - or names its client only in `X-Real-IP` or `Forwarded`, the platform logs one warning per process naming the peer that relayed it, and the security audit (Settings > Access and security) reports the warning "The audit trail records a proxy's address, not users'" (`network.client_addresses_undeclared_proxy`), naming the latest such peer, while such a request has been seen on any replica in the last day. Set `FORWARDED_ALLOW_IPS` to that proxy's address or CIDR (behind a Kubernetes ingress, the cluster's pod network) in the platform container's environment and restart - uvicorn reads it when the platform starts, so it is not a Settings > Access and security field - and make a proxy that names the client only in `X-Real-IP` or `Forwarded` send `X-Forwarded-For`. Once `FORWARDED_ALLOW_IPS` names the peer, the finding becomes the pass `network.client_addresses_declared_proxy`; one about `X-Real-IP` or `Forwarded` ends a day after the last such request. If no proxy fronts the platform, the clients sent the header themselves and nothing needs to change. With no proxy declared and no such request seen, the audit reports it as information, and as a skipped check while Redis does not answer; under Trust proxy headers or a `FORWARDED_ALLOW_IPS` that trusts every peer, its warning "Forwarded headers are trusted from any peer" covers the audit trail. Neither the warning nor the finding changes what is recorded. See [Client address and protocol behind the proxy](installation.md#client-address-and-protocol-behind-the-proxy) for nginx, Traefik and a Kubernetes ingress, and [When the platform reports an undeclared proxy](installation.md#when-the-platform-reports-an-undeclared-proxy).

## URLs

| Variable | Required | Default | Description |
|---|---|---|---|
| `BASE_URL` | **Yes** | `http://localhost:4000` (Docker Compose) | Public URL of the platform. Used for OAuth/SAML callbacks and email links, and as the issuer of the tokens XTM One signs for OpenCTI, OpenAEV and OpenCRQ - set the same value on the worker. Includes the path prefix when `BASE_PATH` is set (`https://intranet.example.com/xtm-one`). Left unset outside Docker Compose, it falls back to `http://localhost:8100`; unset or empty, it is warned about at startup unless `DEBUG=true`. |
| `FRONTEND_URL` | **Yes** | `http://localhost:4000` | Public URL of the frontend. Usually same as `BASE_URL`. |
| `XTM_AUTH_ACCEPTED_AUDIENCES` | No | — | Comma-separated URLs the connected platforms reach XTM One on when they differ from `BASE_URL` (`http://xtm-one:4000` in Docker Compose), accepted as the audience of their tokens. An entry that is not an http(s) URL, or that carries credentials, a query or a fragment, fails startup. See [Public and internal URLs](xtm-suite-integration.md#public-and-internal-urls). |
| `BASE_PATH` | No | — (served at `/`) | URL path prefix when a reverse proxy serves the platform somewhere other than the root of its host: `/xtm-one` for `https://intranet.example.com/xtm-one/`. Works whether the proxy forwards the prefix or strips it (an `X-Forwarded-Prefix` header naming the prefix marks a stripped request). `BASE_URL` and `FRONTEND_URL` must include the same prefix. A plain path of ASCII letters, digits, `-`, `_`, `.`, `~` (no percent-encoding); a prefix colliding with a namespace the platform serves itself (`/api`, `/v1`, `/mcp`, `/xtm`, `/a2a`, `/office`, `/metrics`, `/assets`, `/.well-known`) or with the name of one of its static files (`/favicon.ico`, `/logos`, `/robots.txt`, ...) is refused at startup; one that is also the address of a platform page (`/tools`, `/agents`, ...) is refused unless `BASE_PATH_MODE` declares the proxy. See [Serving under a path prefix](installation.md#serving-under-a-path-prefix). |
| `BASE_PATH_MODE` | No | `auto` | What the reverse proxy does with `BASE_PATH`. `auto` infers the shape of every request (the `X-Forwarded-Prefix` header, then the path) and refuses a `BASE_PATH` that is also a platform page, which the inference cannot resolve behind a stripping proxy. `forwarded` declares that the proxy keeps the prefix; `stripped` declares that it removes it and sends `X-Forwarded-Prefix` (Traefik's `StripPrefix` does, nginx needs `proxy_set_header X-Forwarded-Prefix /xtm-one;`). Either declaration lifts the refusal. |

## Platform mode

| Variable | Required | Default | Description |
|---|---|---|---|
| `PLATFORM_MODE` | No | `xtm_one` | `xtm_one` is the open standalone platform. Set `ai_disabled` to start with AI switched off — see [Running without AI](../administration/running-without-ai.md). |
| `PLATFORM_REGISTRATION_TOKEN` | No | — (registration disabled) | Shared secret OpenCTI, OpenAEV and OpenCRQ present (`Authorization: Bearer`) to register with XTM One - registration is also their heartbeat - and to unregister. Must match across services. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Unset, every registration is refused. `docker-compose.yml` falls back to the published value `xtm-default-registration-token`, accepted only so the platforms already registered stay connected - it cannot register a new platform, change a registered platform's URL, rename one, change what one declares or unregister one - and logged as a warning at startup and reported as a critical finding by the [security audit](../administration/settings.md#security-audit) (`Settings` → `Security`) until you set your own; meanwhile the Filigran license certificate is withheld from registration answers. After 10 failed attempts in a minute, a client is refused (HTTP 429) until the minute ends. See [Upgrade](upgrade.md#platform-registration-token-docker-compose) and [XTM Suite integration](xtm-suite-integration.md#how-the-products-connect). |
| `ENTERPRISE_LICENSE` | No | — | Enterprise license PEM. Can also be configured from Settings UI. Set it on the platform and on the worker: a worker without it runs its background AI on the licensed verdict the platform publishes, and logs a warning once. |
| `AI_UNAVAILABLE_HOLD_MAX_SECONDS` | No | `604800` (one week) | How long background AI work that comes due while AI is unavailable (Community Edition, or AI switched off) is held before it is failed with the reason. Held work (a delayed or queued assignment run, a run resumed from a human-review pause, a background task that had not started, the pending runs of an evaluation) is shown as **Held**, is never charged, and runs by itself within about five minutes of AI being available again. The same bound applies to a due assignment run or background task held while the quota store cannot be reached and quotas fail closed. `0` fails such work at its start instead of holding it. |
| `PLATFORM_NAME` | No | Mode default | Display name override. DB value takes precedence. |
| `PLATFORM_LOGO` | No | Mode default | Logo override (data URI). DB value takes precedence. |
| `BRAND_ASSETS_EXTRA_DIRS` | No | — | Comma-separated folders holding more brand asset packs (`<pack>/manifest.json` next to its files) for the built-in templates, read after the packs the image ships. For a deployment that adds packs of its own; a pack a template includes that no folder holds keeps that template's seeded assets as they are until it is there again. |

## Database

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | No | Built from `DB_PASSWORD` | Full asyncpg connection string. Override when using external PostgreSQL. |
| `DB_PASSWORD` | Yes (if bundled) | — | Password for the bundled PostgreSQL container. |
| `DB_POOL_SIZE` | No | `15` | Connections per worker process. |
| `DB_MAX_OVERFLOW` | No | `10` | Extra connections allowed beyond pool size. |
| `DB_POOL_TIMEOUT` | No | `30` | Seconds a request waits for a free pooled connection. |
| `DB_POOL_RECYCLE` | No | `1800` | Seconds after which a pooled connection is replaced, so connections a firewall or proxy dropped silently are not reused. |
| `DB_CONNECT_TIMEOUT` | No | `10` | Seconds allowed to open one connection. |
| `DB_CONNECT_RETRY_SECONDS` | No | `15` | How long a new connection keeps being retried while PostgreSQL refuses or drops it (restart, failover, a Service briefly without a ready endpoint). Requests and LLM trace writes wait instead of failing; past it requests get a `503` with `Retry-After` instead of a `500`. A timed-out connect is never retried. `0` disables. |
| `MIGRATION_LOCK_TIMEOUT_SECONDS` | No | `300` | Longest wait for a lock by a statement of the database migrations at startup. Past it the migration is retried, then startup fails with an error instead of waiting forever. It replaces a `lock_timeout` set on the database role or the database (`ALTER ROLE ... SET`) for the migration connection, a tighter one included; `0` keeps that one, or no limit when none is set. |
| `MIGRATION_STATEMENT_TIMEOUT_SECONDS` | No | `1800` | Longest run of a statement of the database migrations at startup. Past it the migration is retried once, then startup fails with an error naming this setting. Raise it if an upgrade of a very large database fails on a statement timeout. Like the lock timeout, it replaces a `statement_timeout` set on the role or the database; `0` keeps that one. |
| `STARTUP_WATCHDOG_SECONDS` | No | `300` | When the platform has not completed its startup after this many seconds, it logs an error naming the step in progress and the database sessions blocking the migration, then repeats it at the same interval. After the same delay, the worker's wait for the database migration turns into an error when no platform process is migrating the database. `0` disables. |
| `PG_MAX_CONNECTIONS` | No | `200` | PostgreSQL `max_connections` (Docker Compose managed only). |
| `PG_SHARED_BUFFERS`, `PG_EFFECTIVE_CACHE`, `PG_WORK_MEM`, `PG_MAINTENANCE_WORK_MEM` | No | See `docker-compose.yml` | Memory settings of the bundled PostgreSQL container (Docker Compose managed only). Size them to the memory you give the database. |

## Redis

| Variable | Required | Default | Description |
|---|---|---|---|
| `REDIS_URL` | No | `redis://redis:6379` | Redis connection string. Override for external Redis. |
| `REDIS_PASSWORD` | No | — | Redis password (if required). |
| `REDIS_MODE` | No | `standalone` | `standalone` or `sentinel`. |
| `REDIS_SENTINEL_HOSTS` | No | — | Comma-separated Sentinel addresses (host:port). |
| `REDIS_SENTINEL_SERVICE` | No | `mymaster` | Sentinel service name. |
| `REDIS_SENTINEL_PASSWORD` | No | — | Password of the Sentinel nodes, separate from the Redis password. |
| `REDIS_USERNAME` | No | — | Redis ACL user. In standalone mode it can also be part of `REDIS_URL`. |
| `REDIS_DB` | No | `0` | Redis database number. |
| `REDIS_KEY_PREFIX` | No | — | Prefix of every key, job queue and pub/sub channel the platform and the workers use, such as `xtm-one-prod:`, to share a Redis with other platforms or applications. A Redis user limited to the prefix is then enough: `ACL SETUSER xtm-one on >password ~xtm-one-prod:* &xtm-one-prod:* +@all`. No spaces and none of `* ? [ ] \`. Give the platform and the workers the same prefix. Changing it starts from an empty Redis: queued jobs and the other keys stay under the former prefix. |
| `REDIS_SSL` | No | `false` | TLS to Redis in Sentinel mode. In standalone mode use a `rediss://` URL instead. |
| `REDIS_SSL_CA_CERTS` | No | — | Path of the CA bundle that verifies the Redis certificate. |
| `REDIS_SSL_CERT_REQS` | No | `required` | Certificate check: `required`, `optional` or `none`. |
| `REDIS_MAX_CONNECTIONS` | No | `50` | Connections in the shared pool of each process. |
| `REDIS_SOCKET_CONNECT_TIMEOUT`, `REDIS_SOCKET_TIMEOUT` | No | `5`, `5` | Seconds allowed to connect, and to wait for an answer. |
| `REDIS_MAXMEMORY` | No | See `docker-compose.yml` | Memory cap of the bundled Redis container (Docker Compose managed only). |

!!! warning "Redis memory"
    Redis evicts the least recently used keys when it is full. Durable records live in PostgreSQL, but Redis also holds queued document jobs, recent conversation summaries, single-use links (invitations, password resets) and usage counters not yet written to the database. Give it enough memory that it does not evict under normal load.

## File storage (S3 / Silo)

| Variable | Required | Default | Description |
|---|---|---|---|
| `S3_ACCESS_KEY` | Yes | — | S3 / Silo access key. |
| `S3_SECRET_KEY` | Yes | — | S3 / Silo secret key. |
| `S3_ENDPOINT` | No | `minio:9000` | S3-compatible endpoint. |
| `S3_BUCKET` | No | `copilot-files` | Bucket name. |
| `S3_USE_SSL` | No | `false` | Enable TLS for S3 connections. |
| `S3_REJECT_UNAUTHORIZED` | No | `true` | Verify the certificate of the S3 endpoint. Set `false` only for a self-signed test endpoint. |
| `S3_CONNECT_TIMEOUT`, `S3_READ_TIMEOUT` | No | `10`, `60` | Seconds allowed to connect, and between two received bytes. Large transfers are not cut; only a stalled endpoint is. |
| `S3_REGION` | No | — | AWS region (required for AWS S3). |
| `S3_USE_AWS_ROLE` | No | `false` | Take AWS credentials from the pod or instance role (EKS IRSA or Pod Identity, ECS task role, EC2 instance profile) instead of `S3_ACCESS_KEY` / `S3_SECRET_KEY`, which are then ignored. The role needs `s3:GetObject`, `s3:PutObject` and `s3:DeleteObject` on the bucket objects, `s3:ListBucket` for the bucket check, `s3:GetBucketLocation` when `S3_REGION` is empty, and `s3:CreateBucket` if the platform creates the bucket. Presigned URLs carry the role session token, so they expire with that session (1 hour by default on EKS) even when `S3_PRESIGNED_EXPIRY` is longer. |

## AI providers

Pre-provision LLM providers via environment variables so the platform is ready without UI configuration.

| Variable | Required | Default | Description |
|---|---|---|---|
| `LLM_PROVIDER_{NAME}_API_KEY` | No | — | API key for the named provider. Re-applied on every startup (env wins). The provider shows an **Env** badge in **Settings → AI Models**; removing the variable and restarting is what removes it (deleting it from the UI, where that is allowed, only lasts until the next restart). |
| `LLM_PROVIDER_{NAME}_MODELS` | No | — (`filigran_gateway`: its chat models) | Comma-separated list of models to expose. Either pinned or an editable baseline, depending on `LLM_PROVIDER_MODELS_EDITABLE`. When unset, the provider's stored model list is never touched by the startup seed, except for `filigran_gateway`: when no other provider offers a chat model (and, with `LLM_PROVIDER_MODELS_EDITABLE=true` or `BYOLLM_ACTIVATED=true`, the gateway has no model list yet), startup lists the gateway's chat models and uses them as the list. From then on the list is refreshed on every startup, even once another provider offers chat models, until an administrator edits it (possible with either of those two variables). A model the gateway stops listing leaves the list, but agents and users that picked it keep their choice. If the gateway does not answer, the stored list is kept. A provider that lists no model chats on its built-in chat model only while no configured provider lists one: next to providers whose models are listed, it takes no chat and no failover. A key set for the image, video, speech or music models alone (`DEFAULT_*_PROVIDER` below) therefore needs no model list. |
| `LLM_PROVIDER_MODELS_EDITABLE` | No | `false` | `false`: the `*_MODELS` list **is** the catalog — it overwrites the provider's models on every startup, the **Models** editor is hidden for env-seeded providers and the API refuses model changes on them (403). `true`: the `*_MODELS` list is a **baseline** — seeded on first startup, then only its changes are applied on later restarts (models added to the variable are added, models removed from it are removed, every other edit made in **Settings → AI Models** is kept), and administrators can curate the models of env-seeded providers from the UI. The API key stays env-managed and the provider still cannot be deleted from the UI. |
| `MODELS_ENV_PROVIDING_ONLY` | No | `false` | When `true`, all LLM provider management from the UI/API is disabled (add, edit, rename, delete return 403); providers are defined only through `LLM_PROVIDER_*` variables. Overrides `LLM_PROVIDER_MODELS_EDITABLE`, and is itself overridden by `BYOLLM_ACTIVATED`. |
| `BYOLLM_ACTIVATED` | No | `false` | **Bring your own LLM** — the administrator owns the LLM configuration. When `true`: administrators add and manage providers and their keys from **Settings → AI Models** (implies `LLM_PROVIDER_MODELS_EDITABLE=true`, overrides `MODELS_ENV_PROVIDING_ONLY=true` with a startup warning naming both), and every `DEFAULT_*` value **furnishes an install once** — applied at startup only to an install that has no LLM configuration of its own yet, and inert from the first thing an administrator stores, so no restart can take a choice back (including the choice to leave a field empty). The matching controls stay editable with no **Env** badge, embedding and image defaults included. Provider API keys stay environment-managed either way. |
| `DEFAULT_MODEL` | No | First configured | Platform-wide default model. Re-applied on every startup when a configured provider still offers it (a warning is logged otherwise and the current default is kept). Under `BYOLLM_ACTIVATED=true` it only furnishes an install that has no LLM configuration yet. |
| `DEFAULT_IMAGE_PROVIDER` | No | — | Provider type images are generated with (`openai`, `gemini`, `elevenlabs`, `custom_openai`, `litellm`). Re-applied on every startup with `DEFAULT_IMAGE_MODEL`, and **Settings → AI Models → Image Generation** shows an **Env** badge. |
| `DEFAULT_IMAGE_MODEL` | No | The provider's built-in model | Image model (for example `gpt-image-2`). Set alone, only the model is re-applied, on whichever provider serves it. |
| `DEFAULT_IMAGE_PROVIDER_BASE_URL` | No | — | Base URL of a `custom_openai` or `litellm` image endpoint, composed with that provider's API key. |
| `DEFAULT_VIDEO_PROVIDER` | No | — | Provider type the Video Studio generates AI clips with (`vertex_ai`, `kling`, `runway`, `seedance`, `gemini`, `elevenlabs`, `openai`, `azure_openai`, `litellm`, `custom_openai`, ...). It must be configured, by an `LLM_PROVIDER_{NAME}_API_KEY` or in **Settings → AI Models** (`vertex_ai`: `LLM_PROVIDER_VERTEX_AI_API_KEY` holds the service account key, its JSON or that JSON base64-encoded, with an optional `"location"` member, `us-central1` otherwise); its video model does not have to be in `LLM_PROVIDER_{NAME}_MODELS`. Re-applied on every startup with `DEFAULT_VIDEO_MODEL`, and **Settings → AI Models → Video Generation** shows an **Env** badge. A provider type that does not generate videos, or one with no built-in video model and no `DEFAULT_VIDEO_MODEL`, is logged at startup and ignored: the stored choice is kept and the section stays editable. A provider type that generates videos but is not configured is logged too, and the section stays locked until that provider is configured and the platform restarts. When unset, clips use a provider added for video (Kling AI, Runway, Seedance, Google Vertex AI) first, else the first provider with a built-in video model. |
| `DEFAULT_VIDEO_MODEL` | No | The provider's built-in model | Video model of `DEFAULT_VIDEO_PROVIDER` (for example `veo-3.1-generate-001` on `vertex_ai`, `gemini-omni-1.1-flash` on `gemini`). Required for a provider with no built-in video model (a LiteLLM proxy, an Azure deployment, a custom endpoint, ElevenLabs). Ignored, with a warning, when `DEFAULT_VIDEO_PROVIDER` is not set. |
| `DEFAULT_SPEECH_PROVIDER` | No | — | Provider type that reads answers aloud in the chat and narrates Video Studio videos (`openai`, `gemini`, `elevenlabs`, `azure_openai`, `litellm`, ...). Same rules as `DEFAULT_VIDEO_PROVIDER`; locks **Settings → AI Models → Speech Generation**, voice included. When unset, a voice generator such as ElevenLabs speaks first, else the first provider with a built-in speech model. |
| `DEFAULT_SPEECH_MODEL` | No | The provider's built-in model | Speech model of `DEFAULT_SPEECH_PROVIDER` (for example `tts-1-hd` or `gpt-4o-mini-tts` on `openai`). |
| `DEFAULT_SPEECH_VOICE` | No | The model's first voice | Voice id or name of `DEFAULT_SPEECH_PROVIDER` (an ElevenLabs voice id, `alloy` on OpenAI, ...). People can still choose their own voice where the chat offers it. |
| `DEFAULT_MUSIC_PROVIDER` | No | — | Provider type the Video Studio composes AI music with (`gemini`, `elevenlabs`, `filigran_gateway`). Same rules as `DEFAULT_VIDEO_PROVIDER`; locks **Settings → AI Models → Music Generation**. When unset, the first provider with a built-in music model composes. |
| `DEFAULT_MUSIC_MODEL` | No | The provider's built-in model | Music model of `DEFAULT_MUSIC_PROVIDER` (for example `lyria-3.5` on `gemini`; name it on `filigran_gateway`, which has no built-in model). |
| `DEFAULT_DECISION_PROVIDER` | No | — | Provider type serving the decision model agents use to act on the host page of an embedded chat (`openai`, `openrouter`, `ollama`, `filigran_gateway`, `custom_openai`, and `lmstudio` once an LM Studio integration feeds it). Requires `DEFAULT_DECISION_MODEL`; locks **Settings → AI Models → Decision Model**. Without `DEFAULT_DECISION_MODEL`, or with another provider type, it is logged at startup and ignored, and the section stays editable. When unset, agents cannot act on the host page until an administrator picks a decision model. |
| `DEFAULT_DECISION_MODEL` | No | — | Decision model of `DEFAULT_DECISION_PROVIDER`. Decision models are seldom listed by their provider, so the id is used as written. |
| `ANTHROPIC_THINKING_REPLAY_ENABLED` | No | `false` | Hands the thinking blocks of the current tool round back to the Anthropic models that think by default (Claude Sonnet 5, Opus 5, Fable 5, Mythos), so a tool round resumes with the reasoning that asked for the tools. A turn's blocks are re-sent unmodified only while the system prompt, the tools and the earlier messages are exactly those they were produced under; any change (a tool group loaded mid-run, an older tool result cleared, a compaction) sends the turn without them, as when the setting is off. A request the API refuses over them is sent again without them, so the setting never fails a turn. Every LLM generation whose request carries earlier thinking records what was re-sent and why not in its `thinking_replay` trace metadata, with the setting off too - compare a period with the setting on against one with it off before enabling it for good. |
| `DEFAULT_EMBEDDING_PROVIDER` | No | The first configured of OpenAI, Gemini, Azure OpenAI, Mistral, Bedrock and Ollama | Provider type knowledge bases and memory are embedded with. Re-applied on every startup with `DEFAULT_EMBEDDING_MODEL`, and **Settings → AI Models → Embedding** becomes read-only. |
| `DEFAULT_EMBEDDING_MODEL` | No | The provider's built-in model | Embedding model. Changing it later means every document is embedded again. |
| `DEFAULT_EMBEDDING_PROVIDER_BASE_URL` | No | — | Base URL of a `custom_openai`, `litellm` or `tei` embedding endpoint, composed with that provider's API key. |
| `DEFAULT_EMBEDDING_DIMENSIONS` | No | The model's own size | Smaller vector size, for models that support it (for example `1536` instead of `3072`). |
| `DEFAULT_ROUTING_MODEL` | No | The default model | Model that decides mandatory handovers between agents. Locks its control in **Settings → AI Models**. |
| `DEFAULT_FALLBACK_MODEL` | No | — | Model tried first when the main model keeps failing (rate limit, overload). Locks its control in **Settings → AI Models**. |
| `DEFAULT_REASONING_EFFORT` | No | — (the provider's default) | Default reasoning effort of reasoning models: `low`, `medium`, `high` or `max`. Locks its control in **Settings → AI Models**. |
| `DEFAULT_GENERAL_ASSISTANT_MODEL` | No | `Agent default` | Chat model of the General Assistant. Locks its control in **Settings → General Assistant**. |
| `PROMPT_CACHING_ENABLED` | No | `true` | Anthropic prompt caching: the repeated start of each request (tools, instructions, earlier messages) is billed at the cache rate. |
| `PROMPT_CACHE_TTL` | No | `5m` | How long a cached prefix lives: `5m` or `1h` (twice the write cost, worth it for conversations with long pauses, such as approvals or channels). |

Supported provider names: `OPENAI`, `ANTHROPIC`, `GEMINI`, `OPENROUTER`, `OLLAMA`, `MISTRAL`, `AZURE_OPENAI`, `BEDROCK`, `LITELLM`, `CUSTOM_OPENAI` and `FILIGRAN_GATEWAY`.

The credential of `FILIGRAN_GATEWAY` is `url[||target_provider[||token]]`, with the URL first (unlike `CUSTOM_OPENAI`, which is `api_key||base_url`). Without a token the platform signs an XTM JWT on every request, so the gateway must trust this platform's `BASE_URL` as an issuer (the platform serves its public keys at `{BASE_URL}/xtm/auth/jwks`); with a token, the token is sent instead. With a token and no target provider, write `url||||token`.

!!! tip "Managed deployments: operator baseline, admin curation"
    Set the credential and a baseline catalog from the environment, then let the tenant administrator curate which models are actually offered from **Settings → AI Models**:

    ```
    LLM_PROVIDER_ANTHROPIC_API_KEY=sk-ant-...
    LLM_PROVIDER_ANTHROPIC_MODELS=claude-opus-5,claude-sonnet-5,claude-haiku-4-5
    LLM_PROVIDER_MODELS_EDITABLE=true
    ```

    Adding a model to `LLM_PROVIDER_ANTHROPIC_MODELS` rolls it out to every install on the next restart; removing one retires it everywhere (agents, user preferences and platform defaults that pointed at it are reset). Models the administrator enabled or disabled on top of the baseline are preserved across restarts.

!!! tip "Bring your own LLM: the tenant owns the configuration"
    To ship a working baseline and let the tenant replace it entirely — their own provider, their own key, their own models, their own platform default:

    ```
    BYOLLM_ACTIVATED=true
    LLM_PROVIDER_OPENAI_API_KEY=sk-...        # optional starting point
    LLM_PROVIDER_OPENAI_MODELS=gpt-4.1
    DEFAULT_MODEL=gpt-4.1                     # seeds the default once
    ```

    The administrator can then add any provider from **Settings → AI Models**, curate its models and switch the **Agent LLM Model** to one of them. The built-in agents carry no model of their own, so they follow that platform default immediately. Every restart keeps the administrator's configuration as it stands. Removing `LLM_PROVIDER_OPENAI_API_KEY` still removes that provider on the next restart — credentials remain environment-managed.

    The unit is the install, not the field. An install counts as configured as soon as it stores an agent default model, a routing or fallback model, a reasoning effort, an embedding or image model, or a General Assistant chat model; from then on the `DEFAULT_*` variables are inert on it. An agent, embedding or image model the platform picked by itself from an environment provider (its first chat model, its built-in embedding or image model) does not count: a `DEFAULT_MODEL` added after a first start with provider keys alone is still applied. On a configured install an empty field stays empty — a cleared fallback model, embedding or image model, a General Assistant left on **Agent default** — and re-applying the baseline means clearing the whole configuration.

## Performance

| Variable | Required | Default | Description |
|---|---|---|---|
| `WEB_WORKERS` | No | `2` | Uvicorn worker processes. Production: `2 × CPU cores + 1`. |
| `MAX_REQUESTS` | No | `10000` | Requests a web worker receives before it is replaced (`0` = never), plus up to `MAX_REQUESTS_JITTER` (default half of it). A worker past its quota keeps serving until no agent turn runs in it, so a recycle never cuts a chat turn; the pod stays up and the replacement starts at once. The replacement skips the migrations and the seeding already applied since the container started, and holds the requests it receives while it starts (see [Health probes](monitoring.md#health-probes)). |
| `MAX_REQUESTS_DEFER_SECONDS` | No | `14400` | How long a worker past its `MAX_REQUESTS` quota waits for the agent turns running in it (chat turns, wakes after background work, channel messages) before it recycles anyway. `0` recycles at the quota, cutting the turns still running after `GRACEFUL_TIMEOUT`. |
| `GRACEFUL_TIMEOUT` | No | `120` | Seconds a stopping web worker (a recycle, a deployment, a scale-in) gives the requests and agent turns it runs to finish. A chat turn still running then is cut: it stores a reply naming what stopped it and the tool calls it had completed, and the conversation offers to resume it. |
| `WORKER_CONCURRENCY` | No | `20` | Concurrent queued jobs (document processing and indexing) per worker container. Agent runs are bounded separately by `MAX_CONCURRENT_RUNS` and `MAX_CONCURRENT_SUB_AGENT_TASKS`. |
| `WORKER_JOB_TIMEOUT` | No | `600` | Longest run, in seconds, of a queued worker job. Document processing has its own, longer bound. |
| `WORKER_SHUTDOWN_GRACE_PERIOD` | No | `20` | Seconds a stopping worker gives its in-flight jobs to finish. Keep it about 15 seconds below the container's kill deadline (`terminationGracePeriodSeconds` in Kubernetes, `stop_grace_period` in Docker Compose, both 30 seconds by default). `0` cancels them at once. |
| `APP_MAX_CONCURRENCY` | No | `200` | Requests one platform process handles at once. Above it, requests get a fast `429` instead of queuing; health probes are exempt. `0` disables it. |
| `CHANNEL_MAX_CONCURRENT_PROCESSING` | No | `10` | Incoming channel messages (Slack, Teams, WhatsApp, Telegram, Discord, Google Chat) one platform process answers at once; the others wait their turn. Keep it well below `DB_POOL_SIZE` + `DB_MAX_OVERFLOW`. |
| `STALE_ASSIGNMENT_WARN_SECONDS` | No | `600` | A run still active after this long logs a warning. `0` disables the warning. |
| `STALE_ASSIGNMENT_RUN_SECONDS` | No | `900` | A run nobody is working on any more (its worker or platform process stopped) is failed after this long, and what it held is released. |
| `MAX_CONCURRENT_RUNS` | No | `10` | Max parallel assignment executions. |
| `MAX_CONCURRENT_SUB_AGENT_TASKS` | No | `5` | Max parallel sub-agent tasks. |
| `SUB_AGENT_LEASE_SECONDS` | No | `900` | The lease a background task (a sub-agent delegation) runs under, renewed every third of it. At least three times the longest tool call of an agent turn, so a single long call (a wait, a CI poll, a large read) never costs the task its attempt; a worker that died is recovered after that long. `0` uses `ORCHESTRATION_LEASE_SECONDS`. |
| `MAX_CONCURRENT_AUTONOMOUS_RUNS` | No | `12` | Dedicated slots for autonomous attack-path runs, separate from the assignment/sub-agent pool so dozens can run in parallel without starving normal execution. `0` shares the pool. |
| `AUTONOMOUS_BURST_MAX_CYCLES` | No | `8` | Max decision cycles an autonomous run chains back-to-back before briefly yielding, so a multi-step engagement doesn't stall between cycles. |
| `AUTONOMOUS_BURST_BUDGET_SECONDS` | No | `180` | Wall-clock budget for that back-to-back burst (whichever limit is reached first triggers the yield). |
| `AUTONOMOUS_WAITING_INPUT_RECHECK_SECONDS` | No | `300` | How often a run waiting on operator input re-checks for an answer or stop. Each unanswered re-check doubles the next interval, up to `AUTONOMOUS_WAITING_INPUT_RECHECK_MAX_SECONDS`. `0` waits purely for the operator's reply. |
| `AUTONOMOUS_WAITING_INPUT_RECHECK_MAX_SECONDS` | No | `3600` | Longest interval between two re-checks of a run waiting on operator input. An operator answer still resumes the run at once. |
| `AUTONOMOUS_CANCEL_POLL_SECONDS` | No | `2` | How often the worker re-checks whether an in-flight run was stopped, so an operator Stop aborts the running agent within seconds instead of after the current decision cycle finishes. `0` falls back to the slower lease-heartbeat detection. |

!!! info "PostgreSQL connection budget"
    `(DB_POOL_SIZE + DB_MAX_OVERFLOW) × (WEB_WORKERS × platform replicas + SAQ workers) + 20 < PG_MAX_CONNECTIONS`

    Example with 4 web workers, 1 platform replica and 1 SAQ worker: `(15 + 10) × (4 × 1 + 1) + 20 = 145` → fits the default `PG_MAX_CONNECTIONS=200`. With 2 platform replicas: `(15 + 10) × (4 × 2 + 1) + 20 = 245` → raise `PG_MAX_CONNECTIONS` or lower the pools. During a surge rollout (`maxSurge: 1`) count one more platform replica. The estimate the platform logs at startup covers its own pod only (`WEB_WORKERS` + 1 SAQ worker): with more replicas or SAQ workers, do the full sum.

## Document text extraction

| Variable | Required | Default | Description |
|---|---|---|---|
| `STRICT_BINARY_TEXT_EXTRACTION` | No | `false` | A legacy Word `.doc`, an Outlook `.msg` or an `.xls` that cannot be parsed has no dedicated extractor and is read as text, which keeps it searchable in knowledge bases (the result may carry some noise; a warning is logged once per process). `true` extracts such files to nothing, so a knowledge base sync also removes the ones it had indexed. Zip archives and unreadable PDFs are never indexed as text. |

## Agent run budget

Cost controls for a single agent run (one assignment execution). Each agent run gets a budget object bounding it along four dimensions at once; before every model call the run reserves that call's worst-case spend, debits the actual spend afterwards, and refuses the next call once the budget no longer covers it. A call the remaining budget cannot cover at the model's full maximum output is made with a lower maximum output, sized to what is left (never below 1,024 tokens), and reserved at that size. A slice of the cost/token budget is always held back so the run can still compose a final answer from the work it completed.

Every limit at `0` (the default) means no budget is applied and runs behave exactly as before.

These values do double duty. They are the **default** every run inherits, and they are the **ceiling** a non-admin cannot exceed: an agent's `Budget` tab and a flow's `Budget` node may tighten any dimension freely, but raising one above the value set here requires an administrator. Set them even on a deployment that mostly relies on per-agent budgets — with a dimension left at `0` there is no ceiling for it, so any user may set any value for that dimension on their own agents. See [Run budgets](../usage/agent-budgets.md) for the per-agent and per-flow surfaces and how the layers resolve.

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGENT_LOOP_MAX_COST_USD` | No | `0` | Hard ceiling, in USD, on the model spend of one run. Requires pricing for the model in the platform's price table (seeded automatically for known models); with no price known, cost is not enforced (a warning is logged) and the other limits still apply. Tokens a model reports only in its total - a provider that returns no input / output split, or reasoning tokens left out of the output count - are charged at the higher of the model's input and output prices, so cost is never under-counted (a provider that returns no split at all logs one warning per run). `0` = unlimited. |
| `AGENT_LOOP_MAX_TOTAL_TOKENS` | No | `0` | Hard ceiling on the tokens one run may consume. `0` = unlimited. |
| `AGENT_LOOP_MAX_TOOL_CALLS` | No | `0` | Ceiling on the number of tool calls one run may make: once it is reached, the next model call is refused. Bounds parallel tool use, which the internal iteration limit cannot, but one model response that asks for more calls than are left still runs in full unless `AGENT_LOOP_MAX_TOOL_CALLS_STRICT` is on. `0` = unlimited. |
| `AGENT_LOOP_MAX_TOOL_CALLS_STRICT` | No | `false` | How a tool-call ceiling treats one model response that asks for more calls than the run has left (a parallel batch crossing `AGENT_LOOP_MAX_TOOL_CALLS`, or an agent's or flow's own tool-call limit). `false` runs the whole batch, as before, logs a warning and records the overshoot on the run's trace. `true` runs only the calls that still fit and answers the others "Not executed", so the ceiling is exact. Either way the next model call is refused and the run writes its final answer. |
| `AGENT_LOOP_MAX_DURATION_SECONDS` | No | `0` | Hard ceiling on the wall-clock duration of one run. Combined with the assignment's own timeout — the tighter of the two applies. `0` = unlimited. |
| `AGENT_LOOP_BUDGET_FINAL_ANSWER_RESERVE` | No | `0.1` | Fraction of the cost and token budgets reserved for the run's closing answer, so an exhausted budget still yields a written result instead of a raw digest of tool output. Clamped to `0`–`0.5`. |
| `AGENT_LOOP_BUDGET_FALLBACK_ENABLED` | No | `false` | When the **cost** budget can no longer pay for the next model call, continue the run on the platform's fallback model (Settings → AI Models → Fallback Model / `DEFAULT_FALLBACK_MODEL`) instead of stopping. One downgrade per run. Only cost qualifies — a cheaper model buys no extra tokens, tool calls, or time. An individual agent or flow may turn this on for itself even when it is off here: it cannot raise the cost ceiling, so it is not a privileged change. |

!!! warning "A budget that cannot cover one model call"
    A limit smaller than the cost or tokens of a single call to the configured model, even with its maximum output lowered to 1,024 tokens, makes every run stop immediately and return a summary of tool output. The backend logs an explicit warning at the start of such a run — raise the budget.

!!! info "Finishing on a cheaper model instead of stopping"
    A cost cap of, say, $0.50 on an expensive model usually means "don't spend more than $0.50 *at this quality*", not "abandon the task" — and the same budget buys several times more work on a cheaper model. With `AGENT_LOOP_BUDGET_FALLBACK_ENABLED=true`, a run that reaches its cost budget switches to the fallback model and carries on, once. The rest of the run — including the closing answer — is then charged at the fallback's rates, so the cap still holds.

    It is off by default: finishing a run on a weaker model is a quality decision, so it has to be one you made. Requirements and limits:

    - A fallback model must be configured, and the platform must know its price (pricing is seeded automatically for known models). Without a price there is no way to know the next call is actually cheaper, so the run stops as before.
    - The switch is visible: the run's timeline shows a model-downgrade step, the agent is told its model changed, and every call in the run records which model produced it.
    - The switch is skipped — and the run stops instead — when the fallback would make things worse: its context window cannot hold the agent's instructions, its provider allows fewer tools than the run is using, or the run requires structured JSON output and the fallback is on a different provider. Each case is logged with the reason.

## Write coalescing

When an agent changes the same calendar event several times in one step (the body, then the location), the platform sends ONE update to Microsoft 365 or Google Calendar, so attendees receive one notification instead of one per change. The same applies to several edits of one Teams message. The agent is told which call carried each merged change. It applies to every agent; there is no per-agent setting.

| Variable | Required | Default | Description |
|---|---|---|---|
| `SELF_VERIFICATION_LOOP` | No | `true` | Merge several updates of one calendar event (or edits of one Teams message) asked in the same agent step into one provider call. `false` sends one call per update, as before. The same flag refuses a send (mail, Teams message) whose recipients and body match one the run or the same agent already sent in the last 10 minutes, naming the earlier call; the agent passes `allow_duplicate: true` to send it anyway (audited). After a calendar, mail, chat, work item or presentation write, the agent also reads the object back as the system now shows it, with a list of anomalies of the run (the same object written several times, repeated sends to the same recipients within 2 minutes, a near-identical message, an invitation overlapping another event). |

## Long runs

An agent's `Max iterations` (300 by default, from `Settings > General Assistant`; up to 1000) is the number of tool-calling rounds after which it is asked to wrap up. A run still making progress at that point - its tools succeeded in the last stretch, the stuck-loop breaker below refused nothing and no tool failed repeatedly - is given another stretch of the same size instead, until it is done or reaches 1000 rounds. The reasoning pane says so each time ("Still making progress after N steps"). This applies to chat messages from a signed-in user, the tasks they delegate, and assignment runs. It never applies to the public chat widget, an anonymous channel sender, an evaluation, or a flow node or assignment-delegated task with a pinned number of iterations. A chat message's time limit is sized for the 1000 rounds it may reach, while an assignment run keeps its own timeout, so a long assignment needs a timeout to match. The run budgets above still stop any run, and so does the Stop button. Each stretch also extends the images the run may look at (`AGENT_LOOP_VISION_VIEWS_PER_TURN` per 25 rounds).

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGENT_LOOP_AUTO_EXTEND_ITERATIONS` | No | `true` | Lets a run still making progress at its `Max iterations` continue in stretches of the same size, up to 1000 rounds. `false` makes `Max iterations` a hard cap again, and a chat message's time limit follows it as before. |

## Agent waits

The `wait` tool lets an agent pause while delegated work or CI runs. Two bounds apply to every run (chat, assignments, evaluations): the longest single pause and the total one run may spend waiting. They are set in `Settings > AI > Agent runs`; an agent or an assignment may narrow them with its own wait policy (assignment, then agent, then platform), never above the platform values. Setting either variable below pins it: the Settings control becomes read-only and the admin tool `update_platform_ai_settings` refuses to change it.

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGENT_MAXIMUM_WAIT_TIME` | No | unset (`300` from Settings) | Longest single `wait`, in seconds. Clamped to `1`-`3600`. Unset = the value saved in Settings. |
| `AGENT_MAXIMUM_TOTAL_WAIT_TIME` | No | unset (`1800` from Settings) | Total seconds one run may spend in `wait`. Clamped to `1`-`86400`. Unset = the value saved in Settings. |

## Stuck-loop breaker and vision budget

A model that calls the same tool with the same arguments over and over — re-reading an image whose pixels were pruned, re-running an edit that changes nothing — burns its step budget without progress. The agentic loop counts such calls per turn, warns the model, then refuses the call; a refusal is a tool error the model reads, so it can change approach or report instead of looping. Every trigger is a `loop` span in the run's trace. Only a call whose earlier answer is still in front of the model counts: when the loop itself has cleared an older large result to keep the context focused (the stub tells the model it may re-call the tool), or has compacted it away, the re-read is a fresh ask - a long read of many source files is never mistaken for a loop. A write that took effect starts the count over for every tool that is not itself a write by its name, so the usual write-then-check loop (edit a deck then capture it, act on a desktop then screenshot it, update a record then fetch it through an MCP server) is never mistaken for a loop, whatever the check tool is called; a write repeated with identical arguments still counts. A refused call stays counted whatever later happens to the refusal, and a tool whose own output merely looks like the clearing stub does not earn its caller a fresh count; a model that keeps re-reading cleared files is bounded by the step budget below, not by the breaker. The warning shown in the reasoning pane names the tool, the count and whether the arguments were identical or the target the same. Images are shown to the model at most once per file and per turn, and at most a fixed number of distinct images per turn, whatever tool produced them: a second read of the same file returns the notes the agent saved with `annotate_file`, and a result past the per-turn cap keeps its text and carries an explicit note instead of its pixels, never a silent omission.

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGENT_LOOP_IDENTICAL_CALL_WARN_AFTER` | No | `3` | Identical (tool, arguments) calls in one turn before the model is told to stop and change approach. `0` disables the warning only; the refusal bound below still applies. Set both identical bounds to `0` to disable the identical-call breaker. A warning bound at or past its refusal bound never fires (the call is refused without a nudge) and is reported at startup. |
| `AGENT_LOOP_IDENTICAL_CALL_REFUSE_AFTER` | No | `5` | Identical calls before the call is refused outright. `0` = warn only, never refuse. |
| `AGENT_LOOP_SIMILAR_CALL_WARN_AFTER` | No | `6` | Near-identical calls — the same file or URL read again with different `max_chars` or options — before the warning. A call that names a paging position (`offset`, `cursor`, `page`, ...; `start` only as a number, a date never) not read before on that target is a page, not a repeat: pages are counted apart and warned at three times this value. A re-call whose earlier answer the platform itself cleared from the context is a fresh ask, never a repeat. `0` disables the warning only; set both similar bounds to `0` to disable the similar-call breaker. |
| `AGENT_LOOP_SIMILAR_CALL_REFUSE_AFTER` | No | `10` | Near-identical calls before the refusal; pages of one target are refused at three times this value. `0` = never refuse. |
| `AGENT_LOOP_STEP_BUDGET_NUDGE_FRACTION` | No | `0.6` | Fraction of a run's step budget (between `0` and `1`) at which the model is told which tools consumed its steps and asked to finish or report. `0` disables. |
| `AGENT_LOOP_READ_WINDOWS_PER_FILE` | No | `4` | How many text windows `read_file_content` serves for one file in one turn, per view (the source, or the `mode="extract_structured"` rendering of an HTML, JSON, JSON Lines or CSV file). Past it the call is answered with an explicit error saying the file's window budget is spent for this turn (the file stays attached) and naming the ways forward (the rendered view, or `code_interpreter`, where conversation files are mounted under `attachments/`), never another window. Images are never counted. `0` = no cap; a negative value is refused at startup. |
| `AGENT_LOOP_VISION_VIEWS_PER_TURN` | No | `10` | How many distinct images may reach the model as vision blocks in one turn per 25 steps of its step budget, whatever tool attached them (`read_file_content`, a browser screenshot, a rendered figure, a generated image). The budget follows the steps: an agent whose maximum iterations is 100, or a run its progress extended to 100, may look at four times as many images, so a long visual job (a video, a deck) keeps its sight while it has steps. Enforced by the loop where every tool result is appended: past the cap the result keeps its text and loses its pixels, with an explicit "vision budget exhausted" note naming the files and the notes the agent saved. `0` = no cap; the same file is still shown at most once per turn (a second read returns the saved notes). |

A negative value on any of these fails startup; they are controls, not tuning knobs a typo may silently disable.

## Nova prompt-safety engine

The Nova integration is built on [NOVA](https://github.com/Nova-Hunting/nova-framework) and syncs the [NOVA rules](https://github.com/Nova-Hunting/nova-rules) community collection, two open-source projects by Thomas Roccia released under the MIT license. Connecting it, managing its rules and what it screens are described in [Prompt safety (Nova)](../administration/prompt-safety.md); this section covers the variables that tune its engine.

While the Nova integration is connected and its full engine (`nova-hunting`) is installed, every screened message is evaluated against the enabled rules on the platform's own embedding and LLM providers. The embedding of each rule's `semantics:` pattern is kept in memory per embedding model (provider configuration, model, endpoint and dimensions) and dropped when the rules sync, so only the message itself is embedded on each evaluation. Every distinct `llm:` condition is asked on each evaluation, one model call per condition with the same prompt as before; the calls run a few at a time instead of one after another. Each evaluation logs its provider calls and duration (`Nova evaluation: ...`, with `nova_embedding_calls`, `nova_embedding_cache_hits`, `nova_llm_calls`, `nova_llm_conditions_skipped` and `nova_duration_ms` as fields of the JSON log). The built-in keyword tier, used when the engine is not installed, makes no provider call and reads neither variable.

| Variable | Required | Default | Description |
|---|---|---|---|
| `NOVA_EVALUATION_CONCURRENCY` | No | `4` | How many provider calls one evaluation runs at once: its `llm:` conditions, and the `semantics:` pattern embeddings not kept yet. The same calls are made and give the same verdicts whatever the value; `1` makes them one after another, for a provider that rate-limits parallel calls. Between `1` and `32`; any other value fails startup. |
| `NOVA_LLM_MAX_CONDITIONS` | No | `0` | The most distinct `llm:` conditions one evaluation asks. `0` asks every condition. A ceiling bounds the model calls per screened message, at the cost of detection: the conditions past it are not asked (those of `block` rules are kept first), so a rule resting on one cannot fire through that tier. The platform log warns once per rule set with how many conditions go unasked, and the Security Audit reports the control `agents.nova_llm_every_condition` as off. A negative value fails startup. |

### Semantic-threshold calibration

A rule's `semantics:` pattern carries a threshold - the minimum similarity between the message and the pattern - and the community rules' thresholds were tuned upstream for `all-MiniLM-L6-v2`. Cosine similarities are not comparable across embedding models, so the same threshold can be far too loose or too strict on another model. The platform maps every semantic score onto the reference model's scale before the threshold is compared (`backend/app/services/nova_calibration.py`), so a rule's threshold means the same thing whatever model computes it. The mapping is a measured `baseline` (the model's median similarity for non-matching text) and `span` (the gap to its median matching one), per embedding model. A calibration holds at the vector length it was measured at (384 for the reference model, 768 for `nomic-embed-text`, 3072 for `text-embedding-3-large`): the same model asked for another length through the embedding dimensions setting is scored as before and reported as uncalibrated. The semantic model picked on the Nova rules tab must be one the platform embedding provider can embed with: saving it makes one embedding call and refuses a model that returns nothing, and the choice is kept with the provider configuration it was checked against. A choice made for another provider (checked against another configuration, or, saved before this check, a model of another provider), or one that returns no embedding when a message is screened, is not used: the tier scores on the platform embedding model, so a `block` rule with a `semantics:` pattern is still evaluated, and the tab, `GET /nova-rules/engine-status` (`override_ignored`) and the security audit (`agents.nova_semantic_override_ignored`, a warning) say so. A choice that returned no embedding is tried again after an hour, or as soon as it is saved again.

Calibrations ship for the reference model, the OpenAI and Azure OpenAI default (`text-embedding-3-large`) and the Ollama default (`nomic-embed-text`, and its `latest`, `v1.5` and `137m-v1.5-fp16` tags; Ollama's `all-minilm` is the reference model). A model with no calibration is scored as before and reported as uncalibrated - on the Nova rules tab (a note under the model picker and an *Uncalibrated semantics* badge on every rule with a `semantics:` pattern), on `GET /nova-rules/engine-status` (`semantic_calibration`), and in the [security audit](../administration/settings.md#security-audit) (`agents.nova_semantics_uncalibrated`, an info, or `agents.nova_semantics_uncalibrated_block_rules`, a warning, when an uncalibrated model scores a `block` rule with a `semantics:` pattern). Until a model is calibrated, keep those rules on `Log`.

To calibrate another model, run the harness from `backend/` on a deployment with that embedding provider configured; it embeds the labelled set with the deployment's own provider and prints the entry to add to `CALIBRATIONS` (a credential is never printed):

```bash
python scripts/calibrate_nova_semantics.py                       # the configured model
python scripts/calibrate_nova_semantics.py --model text-embedding-004
```

An `llm:` pattern also carries a number, but it is the temperature of the model's yes/no call (0 to 1), not a confidence threshold: the classifier is binary, so the value changes only the answer's variability. Both `semantics:` and `llm:` numbers are refused on save (REST and the rules tab) unless they are between 0 and 1, which is what the Nova engine accepts.

Synced community rules always arrive with `action = log`; an administrator switches a rule to `block` on the Nova rules tab, and that decision survives every sync (`nova_rule_sync` never resets `action` or `enabled`). A rule whose upstream update is refused on sync (a syntax error, a number outside 0-1) keeps its last version that parsed, with its action, instead of being removed, and a sync that could not read one of the repository's files removes no rule; the rule source line on the tab names the rules kept and the files not read.

## Agentic quota

One agentic execution — a chat turn, an assignment run, a background sub-agent
task, a flow's agent step, a browser-extension task — spends one **agentic
action** from the user's action quota. The action is deducted **before** the
agent runs, which is what lets the platform refuse a request instead of
discovering mid-run that it was never allowed. The consequence is that a run
which ends in failure has already spent an action, on work the user never
received.

The **agent failure pool** gives that unit back, up to a bounded number of
failures per agent:

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGENT_FAILURE_QUOTA_REFUND_ENABLED` | No | `true` | Master switch for the failure pool. On by default — spending an action on a run the platform failed to deliver is the wrong default, and the pool is what keeps that from becoming free unlimited retries. Set it to `false` to deduct an action for every failure, like a success. |
| `AGENT_FAILURE_QUOTA_POOL` | No | `5` | Failures credited back per agent, per user, per pool window. The `N+1`th failure in the same window spends its action normally. `0` closes the pool without touching the switch above. |

How it behaves:

- The first `AGENT_FAILURE_QUOTA_POOL` failed runs of a given agent, for a given
  user, in the current window have their action credited back. Every failure
  after that spends an action like a successful run — so a provider outage or a
  broken tool is forgiven, while an agent that fails every time keeps consuming
  the action quota.
- **The window is one year, on the anniversary of your Filigran license.** With no
  license installed it falls back to the anniversary of this platform instance,
  and if neither is readable, to the calendar year. Settings → Quotas shows
  which anchor is in use and the exact renewal date.
- It is scoped per agent **and** per user: on a company-managed agent shared by
  a whole company, one user's failures never spend another user's pool.
- Failed runs still count as *work performed*: the execution telemetry and the
  run's own record are untouched, and the run is still visible as failed. Only
  the action is credited back.
- **A user whose agentic quota is unlimited is never pooled.** When no tier
  caps the user — the license quotas are at infinity or there is no
  `platform_limit`, the platform default is `Unlimited`, and neither the user
  nor their groups carry a limit — a failed run costs them nothing, so no pool
  slot is spent and nothing is credited back from the pool. Their agents can
  therefore never "exhaust" a pool, and Settings → Quotas shows their rows as
  `Not applicable`. Users and groups with a limit of their own are pooled
  exactly as above; the free credit-back below is unchanged either way.

### When no model was ever called

A run that could not call a model at all — the provider was unreachable, it was
down or overloaded, this platform is not configured / authorised to call it, or
**the platform abandoned the run itself** (the worker was lost, its lease
expired, its deadline blew mid-run) — is a different case: nothing was produced,
and nothing on the user's side caused it. Those runs have their action credited back **for free, outside the pool**:
they never consume a pool slot, and they are never refused, however many of them
happen. One provider outage must not be able to exhaust an agent's whole
allowance and leave real failures charged for the rest of the year.

The free credit-back is **not** gated by `AGENT_FAILURE_QUOTA_REFUND_ENABLED`:
that variable sizes the *pool*, and an unreachable provider is not a pool
question. Turning the pool off still credits back a run that never reached a
model.

What counts as "never reached a model" (`is_provider_unavailable_error` in
`app/llm/router.py`, the single source of truth shared by chat, assignment runs
and extension tasks):

| Failure | Credited back | Paid by |
|---|---|---|
| No LLM provider configured at all | Yes | Free |
| Connection error, DNS failure, timeout | Yes | Free |
| Provider 5xx / 529 overload, gateway 502-504 | Yes | Free |
| Every provider in the failover chain failed for one of the above | Yes | Free |
| Run abandoned by the orchestration engine (worker lost, lease expired, deadline blown mid-run) | Yes | Free |
| Background sub-agent task abandoned the same way | Yes | Free |
| Rate limit / 429 | Yes | The agent's pool |
| Invalid API key, provider not authorised (401/403) | Yes | The agent's pool |
| Provider account out of credit (`insufficient_quota`, billing) | Yes | The agent's pool |
| Context overflow, request too large (413) | Yes | The agent's pool |
| Tool error, malformed model output, any other crash | Yes | The agent's pool |

Everything the provider **replies** sits on the pool side deliberately — a rate
limit, a rejected credential, an account out of credit. They are persistent and
indefinitely reproducible: forgiving them without a bound would stop the action
quota metering entirely for as long as they last, which is the
free-unlimited-retries outcome the pool exists to prevent. They are still
forgiven — out of the bounded pool, which is what bounded is for.

Settings → Quotas reports the two reasons apart — "credited back · agent
failure" (from the pools) and "credited back · provider unavailable" (free) —
with a per-agent breakdown, so a large number can be attributed before anyone
acts on it.

A run the engine abandons is failed from a *different process* than the one
that charged it, so the credit-back cannot rely on anything held in memory: the
charge is recorded on the run itself when it happens, and the abandonment path
reads it back. A run that was never claimed — nobody ever executed it, which is
what a scheduled run does when no worker is running — was never charged, so
there is nothing to credit and nothing appears.

**Background sub-agent tasks** are covered too, by a different mechanism: their
action is charged *inside* the agent turn, where nothing knows about the task, so
the charge writes a short-lived **metering receipt** keyed by the task and the
engine's attempt. The abandonment path redeems that receipt — atomically, so two
sweeps cannot credit twice — which means it credits back only what was really
charged rather than assuming a charge happened.

**A run the engine re-runs** after losing its worker (a restart, or a database
outage long enough for the worker's lease to lapse) is charged one action in
total. Each charge belongs to the attempt that made it: the re-run credits back
what the lost attempt was charged, for free, and is charged its own. The lost
attempt may still be running — losing the lease does not stop it — but it can no
longer credit that charge a second time, and if it only reaches its own charge
after the re-run started, that charge is given straight back.

!!! note "Not yet covered"
    **Flow runs** still are not. A flow can meter several actions (one per agent
    node), so a single receipt per run does not describe it; the mechanism
    extends — one receipt per step — but the abandonment path has to enumerate
    the steps it charged. A worker lost mid-flow therefore still leaves those
    actions charged.

!!! note "Bounded on the generous side"
    A run whose first model calls succeeded and which *then* died on a provider
    outage is also credited back for free — charging a user for an outage they
    could not see is the worse error. Removing that approximation needs
    failures to carry a classifiable cause instead of the free-text error
    string they carry today.
- The General Assistant is not covered — there is no agent to pool against.
- Resetting a user's quota usage from the admin UI also restores their pools.

### Recharging a pool before its renewal date

An administrator can restore every pool on the platform without waiting for the
anniversary, by redeeming a **support code** issued by Filigran in
Settings → Quotas → Agent Failure Pool.

- A code is a single signed token (`XTMP-…`) that the admin pastes in. It is
  verified **offline**, against the same Filigran trust anchor the platform
  already uses for license certificates — an air-gapped deployment can redeem
  one, and no secret is added to the platform.
- Each code carries an expiry and an identifier, and is **single-use**: the
  identifier is recorded in `support_code_redemptions` on redemption, so a
  second attempt (or a code shared between two admins) is refused. Settings →
  Quotas lists the codes already redeemed, when, and by whom.
- A code is normally bound to one `PLATFORM_ID` and refused elsewhere; Filigran
  can also issue a `global` code, redeemable on any platform.
- Because verification is offline, a code cannot be revoked once issued — it can
  only expire. Ask support for a short validity when that matters.

Refusals are shown inline with the reason: not signed by Filigran, altered,
expired on a date, issued for another platform, or already used on a date.

### Resetting the platform-wide usage counters

The same support-code mechanism can reset the **platform-wide usage counters**
the license ceilings are enforced against (`platform_quota_reset` codes,
redeemed from the license banner in Settings → Quotas), for instance when a
proof of concept used the license's allowance before the evaluation ended.

- The reset clears the current day, month and year windows of the agentic and
  sandbox counters by default, in Redis and in `quota_global_usage` alike;
  the code can narrow the windows or include the LLM proxy counter. Past
  windows, per-user usage and the ceilings themselves are left as they are.
- It is single-use, offline-verified and platform-bound exactly like a pool
  recharge, and audited.
- Issuing side: `backend/scripts/mint_support_code.py --kind
  platform_quota_reset [--quota-types …] [--periods …]`.

!!! info "Why a pool instead of refunding every failure"
    Nothing available at the point of failure reliably separates "the provider
    was down" from "this prompt can never succeed". Crediting back every failure
    would make an endlessly retried, hopeless run free; crediting none spends a
    user's action quota on outages they did not cause. A small pool per agent
    covers the accidental case and charges the systematic one to the quota, with
    no classification to get wrong.

!!! note "Sizing is environment-only"
    Both variables are environment-only, by design: what consumes the action
    quota belongs to whoever operates the deployment, not to whoever administers
    the tenant.
    Changing either one requires a restart. The admin screen is read-only apart
    from redeeming a support code.

## Logging

| Variable | Required | Default | Description |
|---|---|---|---|
| `LOG_LEVEL` | No | `info` | `debug`, `info`, `warning`, `error`, `critical`. |
| `LOG_FORMAT` | No | `json` | `json` (structured) or `console` (human-readable). When unset, defaults to `json` in production or `console` when `DEBUG=true`. |

## Email (SMTP)

| Variable | Required | Default | Description |
|---|---|---|---|
| `SMTP_HOSTNAME` | No | — | SMTP server hostname. |
| `SMTP_PORT` | No | `587` | SMTP port. |
| `SMTP_USERNAME` | No | — | SMTP authentication username. |
| `SMTP_PASSWORD` | No | — | SMTP authentication password. |
| `SMTP_FROM` | No | `SMTP_USERNAME` | Sender address for outgoing emails. |
| `SMTP_FROM_NAME` | No | `Filigran Copilot` | Sender display name. Set it to your platform name (`XTM One` in `.env.sample`). |
| `SMTP_STARTTLS` | No | `true` | Connect in plain text, then upgrade with STARTTLS (port 587). |
| `SMTP_USE_TLS` | No | `false` | Encrypt from the first byte (implicit TLS, port 465). Wins over `SMTP_STARTTLS` when both are set. |
| `MODEL_ANNOUNCEMENT_EMAILS_DISABLED` | No | `false` | Turn every model announcement email off for the whole platform: the emails sent when a deployment changes its env-managed catalog (`MODEL_CATALOG_EMAILS_ENABLED`) and the ones an administrator sends from `Settings > AI Models` alike. A boot with it on closes the announcements still queued without sending them, a process with it on sends none, and Settings no longer offers to email the users (the test email included). Model changes are still recorded: once it is off again, a boot with `MODEL_CATALOG_EMAILS_ENABLED` and SMTP sends the latest env change skipped meanwhile, under the same conditions as below. |
| `MODEL_CATALOG_EMAILS_ENABLED` | No | `false` | Email the platform's users when a boot (or switching AI on) finds the env-managed model catalog changed: `DEFAULT_MODEL` moved, an `LLM_PROVIDER_<P>_MODELS` list gained or lost a model, or a capability the environment pins (`DEFAULT_IMAGE_*`, `DEFAULT_VIDEO_*`, `DEFAULT_SPEECH_*`, `DEFAULT_MUSIC_*`, `DEFAULT_DECISION_*`) became available or moved to another model. One email per change names the new default model, the models now available, the ones retired, and each capability with what it does and its model ("Video generation is now available", "New AI capabilities in your <platform> platform" when several arrive at once). A capability the environment stops pinning is not announced, and on the first boot of a release that tracks capabilities, those the platform already had are not announced as new. The video and music models are announced only while the [Video Studio](#video-studio) is on (`ENABLED_FEATURES=VIDEO_STUDIO`), the one feature that uses them; turning it on announces them. While it is off, the speech model is announced for reading answers aloud only, with no word of the Video Studio. Needs SMTP. Whether a change goes out is decided by the boot that records it, from its own configuration, so a pod still running the previous environment during a rolling deploy cannot drop it: a change recorded while this is off (or without SMTP) is sent by a later boot that has both, within 7 days, as long as nobody was emailed about it and it still describes the live catalog. What became of each change (queued, skipped and why, sent to how many) is logged at `WARNING`. A change waits until the env catalog has stayed unchanged for `MODEL_CATALOG_EMAILS_SETTLE_MINUTES`, so the changes of successive deploys reach the users as one email. Emails go out in English from the scheduler, a batch per minute, oldest change first, never twice to the same person: an email whose delivery could not be confirmed (the connection dropped mid-send, a worker stopped) is not retried. Announcements an administrator sends from `Settings > AI Models` do not depend on this switch, only on `MODEL_ANNOUNCEMENT_EMAILS_DISABLED` (see [Emailing users about model changes](../administration/settings.md#emailing-users-about-model-changes)). |
| `MODEL_CATALOG_EMAILS_RECIPIENTS` | No | `all` | `all` (every active user) or `admins`, for the changes a boot finds. Announcements sent from `Settings` go to every active user. The bootstrap `ADMIN_EMAIL` account is never emailed. |
| `MODEL_CATALOG_EMAILS_BATCH_SIZE` | No | `20` | Emails sent per scheduler tick (one a minute); the rest follows on the next ticks. Keep it under your mail server's sending limit: a hosted mailbox allowing 200 emails an hour needs 3 or less. |
| `MODEL_CATALOG_EMAILS_SETTLE_MINUTES` | No | `30` | How long, in minutes (0 to 1440), the env-managed model catalog must stay unchanged before the change a boot found is emailed. Every env change deployed before then (a corrected rollout, a second release, a rolling restart with new values) is added to the same email and restarts the wait, and a change reverted meanwhile is never sent. `0` sends from the next scheduler tick. Announcements sent from `Settings` are never held. |
| `MODEL_CATALOG_CHANGES_WINDOW_HOURS` | No | `24` | How far back, in hours (1 to 168), the model list saves an administrator has not emailed yet are combined into the model changes `Settings > AI Models` offers to email. Every save is kept; a model added then retired within the window (or the reverse) cancels out, and a change the live catalog no longer holds is left out. |

Without `SMTP_HOSTNAME`, the platform sends no email. An administrator's invitation email or password-reset email is refused with an error (an invitation can still be created without email, and its link copied); a user's own `Forgot password` request sends nothing, and writes the link to the log only with `DEBUG=true`.

Every email the platform sends (password reset, invitation, model announcements) follows the Filigran email layout and loads its images from `<FRONTEND_URL>/email-assets/`: the recipients' mail client must reach `FRONTEND_URL` to show the logos.

## Observability

| Variable | Required | Default | Description |
|---|---|---|---|
| `LANGFUSE_HOST` | No | `https://cloud.langfuse.com` | Langfuse server URL. |
| `LANGFUSE_PUBLIC_KEY` | No | — | Langfuse public key. |
| `LANGFUSE_SECRET_KEY` | No | — | Langfuse secret key. |
| `PROMETHEUS_ENABLED` | No | `false` | Enable Prometheus metrics endpoint. |
| `PROMETHEUS_BEARER_TOKEN` | No | — | Bearer token for metrics endpoint (min 16 chars). |
| `TRACING_STORE_PROVIDER` | No | `postgres` | Where LLM traces are kept: `postgres` (the platform database) or `clickhouse`. With `clickhouse`, traces are written to both stores and read from ClickHouse. See [Request tracing](monitoring.md#request-tracing). |
| `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT` | No | `localhost`, `8123` | ClickHouse HTTP address. |
| `CLICKHOUSE_USERNAME`, `CLICKHOUSE_PASSWORD` | No | `default`, — | ClickHouse credentials. |
| `CLICKHOUSE_DATABASE` | No | `default` | ClickHouse database the trace tables are created in. |
| `CLICKHOUSE_SECURE` | No | `false` | HTTPS to ClickHouse. |
| `CLICKHOUSE_CONNECT_TIMEOUT_SECONDS` | No | `5` | Seconds allowed to connect to ClickHouse. |

## Usage telemetry

What is sent, where and how often: [Usage telemetry](telemetry.md).

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEMETRY_ENABLED` | No | `true` | Anonymous usage telemetry to Filigran. `false` turns it off: the platform makes no connection to the collector, not even the startup check. Read at startup. |
| `TELEMETRY_ENDPOINT` | No | Filigran's collector (its staging one with `DEBUG=true` or `PLATFORM_MODE=dev`) | OTLP/HTTP endpoint (`.../v1/metrics`) the metrics are sent to, your own collector for example. |
| `TELEMETRY_TAGS` | No | — | Comma-separated deployment tags (`saas,eu-west`) sent with every metric. Trimmed, lowercased, deduplicated and sorted before they are sent. |

## Security feature flags

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGENT_TOOLS_STRICT_ADMIN_ACCESS` | No | `false` | Keep every agent tool of an admin - the agent builder, the agent trace inspection and the work-items delegation, in chat and on the XTM One MCP server - to the admin's own, group-shared and company-managed objects, like every other caller: the agents, assignments, runs, traces, evaluations, knowledge bases, skills and other entities a tool looks up, lists or binds. The admin role still counts where a tool checks a permission (a company-managed object, an admin-only setting). By default an admin's agent reaches everything, as the admin does in the UI and through the REST API; the Security Audit reports the default while an agent carries one of these tools, chat is on (the General Assistant offers the agent builder in every conversation) or the XTM One MCP server is enabled. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `DISABLE_CODE_INTERPRETER` | No | `true` (xtm_one mode) | Disable code interpreter at infra level. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `DISABLE_CUSTOM_TOOLS` | No | `true` (xtm_one mode) | Disable custom tools at infra level. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `CUSTOM_TOOL_SANDBOX` | No | `auto` | Custom tool execution mode: `auto`, `docker`, `subprocess`, `opensandbox`. `opensandbox` delegates to an OpenSandbox-managed container (also used by the code interpreter) and requires the OpenSandbox connection variables below. |
| `PLATFORM_HEALTH_SPEND_LIMITS_UNLOCKED` | No | `false` | Platform Health spends outside the agentic quota, bounded only by its own spend ceilings (Budget, Analysis and Module Analyst in **Settings > Platform Health**). Unset or `false`: those ceilings stay at their built-in defaults, shown locked, and a change to them is refused; a stored value is kept, not applied. `true`: the stored values apply and the ceilings are editable. Restart required to take effect. |
| `DISABLE_SANDBOX_QUOTA_USAGE` | No | `false` | Opt out of per-user sandbox usage counting (`quota_usage` rows with `usage_type="sandbox"`, alongside the existing LLM-proxy/agentic usage). Counted by default; set `true` for self-hosted/on-prem deployments that manage their own sandbox infrastructure independently of hosted usage-based billing. Restart required to take effect. |
| `INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION` | No | `false` | Refuse every Slack event on `/api/v1/integrations/notifications/slack` and every Microsoft Graph change notification the platform cannot authenticate, those of the Teams bot's private and shared channels on `/api/channels/teams/graph-notifications` included. By default a Slack integration with no usable signing secret keeps receiving its events without a verified signature, and a Graph subscription recorded without a clientState keeps working, each with a warning in the log and a Security Audit finding. A delivery that a stored Slack signing secret or a Graph clientState does not prove never reaches what that secret or clientState protects, whatever the setting. The same setting limits what the admin OAuth app's webhook (`/api/hooks/providers/<provider>`) and the LinkedIn notification URL deliver to the admin app's integrations the agent owner may use (for a GitHub App event, those recording its installation or, recording none, whose own token sees it), and drops a LinkedIn notification when no LinkedIn client secret is configured; by default they reach what they reached before, with a warning and a Security Audit finding. That webhook accepts, without a check, a delivery for a provider whose OAuth default holds no webhook secret, and by default one for a provider with no OAuth default (with the setting on, such a provider reaches nothing); both are reported the same way. For the Teams bot's private and shared channels, a notification that does not carry the subscription id and the random secret (`clientState`) XTM One recorded for its channel is still acted on by default whenever earlier releases acted on it, with a warning and a Security Audit finding, and the subscriptions created before 2026-07-02 (whose secret anyone who knows the team id can compute) are kept and renewed; with the setting on, such a notification is refused, the maintenance pass replaces those subscriptions and the ones whose secret can no longer be decrypted (if Microsoft Graph refuses to create a replacement, that channel's follow-ups stop until a later pass creates it), and what a refused notification held back is read back from Microsoft Graph, up to four hours back. Nothing to set on upgrade. See [Real-time Slack, Microsoft Graph or LinkedIn events do not arrive](troubleshooting.md#real-time-slack-microsoft-graph-or-linkedin-events-do-not-arrive) and [Microsoft Teams](../administration/channels/microsoft-teams.md#graph-notifications-for-private-channels). Also in **Settings > Access and security** (**Require verified integration notifications**); the variable, when set, wins and locks the field. |
| `WORK_ITEM_NOTIFY_TEST_RECIPIENTS` | No | empty | The targets, comma-separated, a test work item may name for its end notification: an address (`payload.notify.user`), a Teams chat id (`payload.notify.chat_id`) or `team_id/channel_id` (a Teams channel). A test work item is one whose correlation key starts with `retest:` or `test:`, or whose title starts with `Notify retest`. Any other recipient is refused when the item is created or updated, so a platform retest never messages a real person. The sending account's own chat (`chat_id` `48:notes`) is always allowed. Empty: a test item may only notify that own chat. |
| `WORK_ITEM_DEPLOYMENT_REPOSITORIES` | No | `XTM-One-Platform/xtm-one` | The repositories (`owner/name`, comma-separated) whose deployments the platform observes. A work item may wait on the deployment of a change (`blocked_on_refs` kind `deployment`) only in one of them, since such a wait is released when a running build carries the merge. A deployment wait on any other repository is refused when the item is saved, with the alternatives: a `workflow_run` wait on that repository's release workflow, or a reason with a start date for the re-check. |
| `WORK_ITEM_STALE_REF_WAIT_DAYS` | No | `7` | How many days a blocked work item waiting on pull requests, issues, deployments or workflow runs may stay quiet before it is shown as stale ("needs a look"). An item whose start date is ahead is never stale on inactivity, and one waiting on a deployment the platform does not observe always is. `0` treats such a wait like any blocked item (the 24 h window). |
| `PROMPT_GUARD_SCANNED_TOOLS` | No | empty | The tools whose results the connected prompt guard screens, comma-separated: a tool's registered name for a built-in, integration or MCP tool (for example `web_fetch,search_jira_issues`), and `custom:<id>` for a custom tool, with the custom tool's id - it stays screened when its owner renames it. While a company-managed Nova integration is connected, the guard evaluates what a listed tool returns to an agent - the text the agent reads, followed by the text of the files the result attaches (images excluded) - and records each rule match in the audit log (`Security alert`, surface `tool_result`, with the tool, the rules and their action). The result itself is left exactly as it is, and the evaluation runs beside the agent, which does not wait for it. Empty by default: no tool result is screened, and an agent run does what it did before the setting existed. Also in **Settings > Access and security** (**Tools screened by the prompt guard**), where the tools are picked from a list; the variable, when set, wins and locks the field. |
| `PROMPT_GUARD_BLOCK_TOOL_RESULTS` | No | `false` | With `PROMPT_GUARD_SCANNED_TOOLS` set, withhold a screened tool result that a `block` rule matches: the agent reads a short refusal naming the tool instead, without the files the result carried, and the result is not reused for an identical call. The agent then waits for the guard's verdict on each screened result before it continues (at most 20 seconds each, the results of one parallel batch evaluated together, four at a time in each platform or worker process); a guard that fails or runs out of time lets the result through. Off, a match is recorded and the result reaches the agent as it is; the [security audit](../administration/settings.md#security-audit) reports that log-only mode while a guard is connected. Also in **Settings > Access and security** (**Withhold tool results a guard rule blocks**); the variable, when set, wins and locks the field. |
| `PUBLIC_WIDGET_STRICT_RENDERING` | No | `false` | Public chat widget: show only the images the platform serves in an answer (any other image becomes a link the visitor can open, so the visitor's browser never fetches a URL an answer chose), open every link of an answer in a new tab, and render answers without inline styles, presentational markup (`align`, `bgcolor`, `<font>`, ...), SVG or MathML, with links of the `http`, `https`, `mailto` and `tel` schemes only. By default answers render as they always did: an image from any `http(s)` host is shown, an HTML link without a target opens in the page, and inline styles, presentational markup, SVG / MathML drawings and links of any other scheme (`msteams:`, `sms:`, ...) are kept. Script is removed in both modes: script elements, event handlers, `javascript:` / `vbscript:` / `data:` URLs, frames, forms and embedded objects. Links that launch a Windows program with a public exploitation history (`ms-msdt:`, `search-ms:`, `ms-officecmd:`, `ms-appinstaller:`, ...) are removed in both modes too, their text kept, as they are in the Office add-in; app links (`slack:`, `msteams:`, ...) and the Office document links (`ms-word:`, `ms-excel:`, ...) keep working. The [security audit](../administration/settings.md#security-audit) (`Settings` → `Security`) reports the default while a widget is enabled. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `PUBLIC_WIDGET_UPLOAD_RETENTION_DAYS` | No | `7` | Public chat widget visitor uploads (see [Visitor uploads on the public chat widget](#visitor-uploads-on-the-public-chat-widget)): the files a visitor attached are deleted once their conversation has had no message for this many days; a file uploaded and never sent with a message is deleted after a day, and the files of a deleted conversation within the hour. A value below `1` is read as `1`: the retention cannot be switched off. |
| `SVG_STRICT_RENDERING` | No | `false` | SVGs a model is shown (a chat upload, a user-message image, a tool result such as an agent reading back the diagram it drew): refuse, instead of rendering, an SVG that declares an XML entity or references anything outside itself - a file path, an `http(s)` URL or another document, in an `<image>`, `<feImage>` or `<use>` or a `url(...)` in an attribute. By default such an SVG renders with that reference dropped - what resvg drew there on its own, as it follows only references inside the document - and an internal entity (an Adobe Illustrator export declares its namespaces this way) is expanded within a bound. Nothing is fetched or read from disk in either mode, and a nested SVG embedded as a `data:` URI (draw.io icons) is checked under the same rules. The [security audit](../administration/settings.md#security-audit) (`Settings` → `Security`) reports the default. |
| `PROMPT_GUARD_BLOCK_GENERAL_ASSISTANT` | No | `false` | While a prompt guard (the Nova integration) is connected, a General Assistant message is evaluated before the model is called and every rule match is written to the audit log (`security_alert`, `source: nova_guard`, `surface: chat_message`, `chat_surface: general_assistant`). By default the message is answered even when a `block` rule matched (the entry records `rule_action: block`, `action: allowed`): the General Assistant was not guarded before, so nothing it answers today is refused on upgrade. Set `true` to refuse such a message with the guard's message, as agent chat always does. The [security audit](../administration/settings.md#security-audit) reports which of the two applies while a guard is connected. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `PROMPT_GUARD_BLOCK_PUBLIC_WIDGET` | No | `false` | The same for a public chat widget visitor's message (`chat_surface: public_widget`). A visitor has no account, so the entry is written as the guard (`system:nova_guard`) and attached to the admin account the widget acts as (`ADMIN_EMAIL`). With `true`, a refused message reaches no model and its agentic action is given back. |
| `PROMPT_GUARD_MODEL_OUTPUT` | No | `off` | Evaluate the model's answer with the connected prompt guard (the Nova integration): `off`, `log` or `block`. Covered: the final answer of every top-level agent turn (web chat, the REST chat API, channel bots, the chat embedded in OpenCTI / OpenAEV, A2A, the MCP agent endpoints, the LLM proxy, the browser extension, background tasks, flows), the General Assistant's (a transfer's included) and the public widget's. Not covered: a consult's or a transfer's inner answer (its parent's answer is), evaluation runs, the OpenAEV autonomous run's cycles after the first, and assignment runs, whose result reaches their owner and which act on the outside world through write tools. `off` evaluates nothing. `log` returns every answer unchanged and at once, and evaluates it in the background (at most 4 at a time per process and 64 pending, 20 seconds each, the first 20,000 characters); a match is written to the audit log (`security_alert`, `surface: model_output`, `action: allowed`). `block` evaluates an answer before it is delivered and replaces one a `block` rule matches with a withheld notice (`action: block`), where the answer is delivered whole: the REST chat API, channel bots, A2A, the MCP endpoints, the LLM proxy, the browser extension, background tasks, the widget's non-streaming endpoint and a transfer from the General Assistant. A streamed answer (web chat, the streaming chat embedded in OpenCTI / OpenAEV, the widget's stream) has already been shown when it is complete, so in `block` it is evaluated and audited after delivery as in `log`, with `streamed: true`, and never withheld. An evaluation that fails or runs out of time delivers the answer. With no guard connected nothing is evaluated whatever the value. Write-tool arguments (an email body, a ticket comment) are not evaluated. The [security audit](../administration/settings.md#security-audit) reports the value while a guard is connected. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |

!!! info "Embedding the chat widget on a site with its own Content-Security-Policy"
    The widget (`/api/v1/public/widget.js`) runs inside the page of the site that embeds it, in a Shadow DOM, so that site's Content-Security-Policy applies to everything it loads. If the site sends one, it must allow:

    - `script-src`: the XTM One origin (the widget script);
    - `connect-src`: the XTM One origin (the API calls and the streamed answers);
    - `img-src`: the XTM One origin (the widget logo and the images the platform serves), plus the hosts answers link images from - by default an answer can show an image from any `http(s)` host, and with `PUBLIC_WIDGET_STRICT_RENDERING=true` it shows only the platform's own;
    - `style-src`: `'unsafe-inline'` and `https://fonts.googleapis.com`;
    - `font-src`: `https://fonts.gstatic.com`.

    `'unsafe-inline'` in `style-src` is the only choice today: the widget styles itself with a `<style>` element it builds at run time and with an inline `style` attribute in its markup, and it supports no nonce. A hash cannot replace it either: the stylesheet changes with every widget release, and a hash does not cover a `style` attribute without `'unsafe-hashes'`. Without it the widget's styles are blocked and it renders unstyled.

    In both rendering modes, an image the platform stores for the visitor's session - a file or a generated image an answer shows by its platform address (`/api/v1/chat/files/<id>`, `/api/v1/chat/images/<id>`) - is shown inline and loads from the XTM One origin through the visitor's own session (`/api/v1/public/chat/<token>/sessions/<session>/...`): the visitor needs no platform account, and the platform refuses any file or image the session does not own.

### Visitor uploads on the public chat widget

A widget visitor has no account (see [Publish an agent on a website](../usage/web-widget.md)). Each agent's widget can let its visitors attach files to their messages: **Allow visitor uploads** in the agent's `Configuration` > `Channels` > `Web Exposure`, off for every widget until its owner turns it on. The [security audit](../administration/settings.md#security-audit) reports the widgets that allow it (control `web.public_widget_uploads`, a warning while one does). With it on, the widget shows an attach button and takes a pasted screenshot or a dropped file. The agent receives each file as in the web chat - a document as its text (the first 50,000 characters, what the chat upload message carries), an image by its pixels, and every file's id for the tools that take a file - but a document never reaches the model as its native bytes, even with a provider that reads PDF or Office files natively: the model reads exactly the text the prompt guard evaluated. What bounds an anonymous uploader:

- **Types**: PDF, Word (`.docx`), Excel (`.xlsx`, `.xls`), PowerPoint (`.pptx`), text and data files (`.txt`, `.md`, `.csv`, `.tsv`, `.json`, `.jsonl`, `.ndjson`, `.xml`, `.yaml`, `.yml`, `.log`, `.html`, `.htm`) and raster images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`), decided on the file name and checked against the file's content: an image is decoded as the format its name says and may declare at most 20,000 pixels a side and 50 million in all, an Office document must be an Office package whose parts and expanded size are bounded (no archive under another name). The type the browser declares is ignored. No SVG, archive, audio or video.
- **Size and load**: the chat upload limit, 40 MB a file. A larger body is refused on its declared length before it is read, and cut off past the limit when it declares none. Each platform process reads at most 4 visitor files at once, uploads and the documents of messages being sent alike; past that a visitor is told to retry (`503` with `Retry-After`), and a message refused this way keeps its files in the widget. Every image and Office file is decoded, and every document read, when it is uploaded, in a worker process of its own, never on the threads the platform's own document work uses: a file that takes more than 30 seconds, or more than 2 GB of address space where the operating system lets that cap be set (Linux), is stopped and refused (`422`). A visitor who disconnects mid-upload still counts against those 4 files until the store ends, and leaves no stored file that the quotas and the retention do not count - unless the object store and the database both fail at that moment, in which case the object's key is written to the error log for an operator to remove.
- **Budgets**: 20 uploads an hour per client address and 200 an hour per widget across all its visitors. Both refuse while Redis cannot be reached, whatever `RATE_LIMIT_FAIL_MODE` says.
- **Storage**: 10 files and 100 MB per conversation, 2 GB per widget across its conversations, checked before the body is read and again when the file is written.
- **Content**: while a prompt guard (the Nova integration) is connected, the text the model will read of each document is evaluated before the file is stored, and a match is written to the audit log (`security_alert`, `surface: widget_upload`, as the guard, attached to the admin account the widget acts as). With `PROMPT_GUARD_BLOCK_PUBLIC_WIDGET=true`, a file a `block` rule matches is refused.
- **Reach**: a visitor's file belongs to its conversation and to no user. Only the visitor's own session downloads it (an image inline, every other file as a download, never kept by a cache); on the platform only administrators can open it.
- **Retention**: deleted once the conversation has had no message for `PUBLIC_WIDGET_UPLOAD_RETENTION_DAYS` (7 by default), a file never sent with a message after a day, and the files of a deleted conversation within the hour - the stored object first, then its record, so an object store that does not answer leaves both for the next hourly pass. Turning the switch off stops new uploads; the files already attached follow the same retention.

Every refused upload says why - its type, its size, a budget, the conversation's or the widget's storage, the content check - and the widget shows the reason next to the file.

## Settings > Access and security options

These security options are set in **Settings > Access and security** by an administrator, or by their environment variable. The rule is the one the AI Models defaults and the Langfuse card follow: **the variable, when set (and not blank), wins** - the field is then shown read-only with the variable's name ("Set by environment variable X") and a change to it is refused. With the variable unset, the value saved in Settings > Access and security applies; with nothing saved, the default below applies, which is the platform's behaviour before the options reached the UI. A change made on one replica applies on every web and worker process within 5 seconds.

One option is fixed in xtm_one mode (and ai_disabled): quotas are part of the Filigran licence, so **Quotas when their store is unreachable** is always `closed` - while the quota store cannot be reached, agentic work is refused rather than run unmetered, requests with no account behind them included (anonymous channel users, the public chat widget, the platform MCP key). A due automation (a scheduled, webhook or integration-event assignment run, a background task) is not refused but held: it is shown as **Held**, is charged once it runs, and runs by itself once the store answers again, bounded by `AI_UNAVAILABLE_HOLD_MAX_SECONDS`. A store that refuses the platform's credentials (a wrong password, an ACL denial) is a configuration error, not an outage: that work is refused at once. The field is shown read-only with that reason and a change to it is refused; `QUOTA_FAIL_MODE` has no effect (set to `open`, it is ignored with a warning at startup), and a value already saved in Settings > Access and security is kept but not applied. In copilot / dev mode the option follows the rule above.

| Settings > Access and security field | Variable | Default |
|---|---|---|
| Trust proxy headers | `TRUST_PROXY_HEADERS` | `false` (prefer `FORWARDED_ALLOW_IPS`) |
| Canonical client addresses | `CLIENT_IP_STRICT` | `false` |
| Require HTTPS for runner enrollment and MCP tokens | `CREDENTIAL_TRANSPORT_STRICT` | `false` |
| IPv6 rate-limit prefix length | `RATE_LIMIT_IPV6_PREFIX` | `128` (32-128) |
| Rate limits when Redis is unreachable | `RATE_LIMIT_FAIL_MODE` | `open` |
| Quotas when their store is unreachable | `QUOTA_FAIL_MODE` | always `closed` in xtm_one mode (not configurable, the variable is ignored), `open` in copilot / dev |
| Minimum password length | `MIN_PASSWORD_LENGTH` | `8` (1-1024) |
| Password reset link lifetime (hours) | `PASSWORD_RESET_EXPIRE_HOURS` | `24` (1-720) |
| Invitation link lifetime (hours) | `INVITATION_EXPIRE_HOURS` | `168` (1-720) |
| Absolute session lifetime (minutes) | `JWT_EXPIRE_MINUTES` | `10080`, 7 days (5-525600 in the field; see [Session lifetime and idle timeout](authentication.md#session-lifetime-and-idle-timeout)) |
| Idle session timeout (minutes) | `SESSION_IDLE_TIMEOUT_MINUTES` | `0`, no idle timeout (0, or 5-525600) |
| API key maximum lifetime (days) | `API_KEY_MAX_LIFETIME_DAYS` | `0` (no maximum; 0-3650). Existing keys keep their expiry; see [API keys](../usage/my-profile.md#api-keys-tab) |
| MCP client self-registration | `MCP_DCR_ENABLED` | `true` |
| MCP client metadata documents | `MCP_CIMD_ENABLED` | `true` |
| Automatic MCP OAuth client registration | `MCP_OUTBOUND_DCR_ENABLED` | `true` |
| Strict OAuth token URL checks | `OAUTH_TOKEN_URL_STRICT` | `false` |
| Loopback token URL origins (development) | `OAUTH_TOKEN_DEV_TRUSTED_ORIGINS` | empty (applies under the strict checks only) |
| MCP server URL policy | `MCP_SERVER_URL_POLICY` | `permissive` (`private_networks`, `public_only`) |
| Trusted MCP server origins | `MCP_SERVER_TRUSTED_ORIGINS` | empty (applies under the stricter URL policies only) |
| Trusted A2A partner origins (development) | `A2A_CLIENT_DEV_TRUSTED_ORIGINS` | empty |
| Accept plain HTTP from loopback (testing) | `A2A_ALLOW_INSECURE_LOOPBACK` | `false` |
| Require webhook secrets | `WEBHOOK_REQUIRE_SECRET` | `false` |
| Exact event matching for every trigger | `WEBHOOK_EXACT_EVENT_MATCHING` | `false` |
| Require verified integration notifications | `INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION` | `false` |
| Strict widget rendering | `PUBLIC_WIDGET_STRICT_RENDERING` | `false` |
| Prompt guards block widget messages | `PROMPT_GUARD_BLOCK_PUBLIC_WIDGET` | `false` |
| Prompt guards block General Assistant messages | `PROMPT_GUARD_BLOCK_GENERAL_ASSISTANT` | `false` |
| Prompt guards on model answers | `PROMPT_GUARD_MODEL_OUTPUT` | `off` (`off`, `log`, `block`) |
| Strict admin access for agent tools | `AGENT_TOOLS_STRICT_ADMIN_ACCESS` | `false` |
| Public presentation links | `PRESENTATION_PUBLIC_LINKS_ENABLED` | `true` (see [Public link](../usage/presentations.md#public-link)) |
| Public video links | `VIDEO_PUBLIC_LINKS_ENABLED` | `true` (see [Public link](../usage/video-studio.md#public-link)) |
| Published dashboard links | `DASHBOARD_PUBLIC_LINKS_ENABLED` | `false` (see [Published link](../usage/custom-dashboards.md#published-link)) |
| Browser profiles on private networks | `BROWSER_PRIVATE_TARGETS` | `false` (see [Browser profiles](#browser-profiles)) |
| Trusted certificates for web fetches | `WEB_FETCH_TRUSTED_CA_CERTIFICATES` | empty (see [Outbound proxy and certificate authorities](#outbound-proxy-and-certificate-authorities)) |
| Scheduled work items | `SCHEDULED_WORK_ITEMS_ENABLED` | `true` (see [Scheduled work items](../usage/scheduled-work-items.md)) |
| Require a PIN on the mobile companion | `COMPANION_PIN_REQUIRED` | `false` (see [Mobile companion](../usage/mobile-companion.md#lock-the-companion-with-a-pin)) |
| Phone notification content | `PHONE_NOTIFICATION_CONTENT` | `full`; `minimal` sends only "New notification" (see [Mobile companion](../usage/mobile-companion.md)) |
| Tools screened by the prompt guard | `PROMPT_GUARD_SCANNED_TOOLS` | empty (no tool result screened) |
| Withhold tool results a guard rule blocks | `PROMPT_GUARD_BLOCK_TOOL_RESULTS` | `false` |
| Strict runner folder confinement | `RUNNER_STRICT_PATH_CONFINEMENT` | `false` |
| Disable the code interpreter | `DISABLE_CODE_INTERPRETER` | `true` in xtm_one mode, `false` in copilot / dev |
| Disable custom tools | `DISABLE_CUSTOM_TOOLS` | `true` in xtm_one mode, `false` in copilot / dev |
| Disable custom variables | `DISABLE_CUSTOM_VARIABLES` | `true` in xtm_one mode, `false` in copilot / dev |
| Code interpreter: re-enabled modules | `CODE_INTERPRETER_EXTRA_ALLOWED_MODULES` | empty |
| Custom tools: re-enabled Python modules | `CUSTOM_TOOL_EXTRA_ALLOWED_MODULES` | empty |
| Node.js permission model for custom tools | `CUSTOM_TOOL_NODE_PERMISSION` | `true` |

A value typed in Settings > Access and security is validated like the variable is at startup (a malformed origin or an out-of-range number is refused). The API behind the card is `GET` / `PUT /api/v1/settings/security-options` (administrators only); every change is recorded in the audit log.

## XTM Hub registration

| Variable | Required | Default | Description |
|---|---|---|---|
| `XTM_HUB_DEFAULT_URL` | No | `https://hub.filigran.io` | The XTM Hub this platform registers with by default, like OpenCTI's `XTM__XTMHUB_URL` and OpenAEV's `OPENAEV_XTM_HUB_URL`. It is the XTM Hub URL proposed in [Settings > Parameters](../administration/settings.md#xtm-hub-registration), the one automatic registration uses, the XTM Hub the product switcher and the home dashboard link to, and the address of an XTM Hub integration that names none. An absolute http(s) URL without credentials, query or fragment; anything else refuses to start. A registration token is only sent to it over `https`, or to an XTM Hub on this machine. |

An XTM One instance deployed in an XTM Platform trial is registered with the
XTM Hub automatically: once it is up, the deployer calls
`PUT /api/v1/xtm-hub/registration/auto` with an administrator API key
(`Authorization: Bearer`, for example the `ADMIN_TOKEN` key) and the body
`{"token": "<platform token>"}`, the token the XTM Hub issued for this instance.
XTM One then registers itself with `XTM_HUB_DEFAULT_URL` under its `PLATFORM_ID`,
which must be the instance ID the XTM Hub knows for this deployment. The call
answers `204` once the XTM Hub accepted the registration, `400` when the token is
not an XTM Hub platform token, and `502` when the XTM Hub refused it or could not
be reached; nothing is stored then. The registration is then checked every hour
like one made from Settings.

## XTM Hub sign-in gate

Restricts sign-in to people XTM Hub has entitled to *this* XTM One instance, and
lets the Hub decide who is an administrator here. OIDC only — XTM Hub does not
provide SAML.

| Variable | Required | Default | Description |
|---|---|---|---|
| `XTM_HUB_ADMIN_GROUP` | No | — | The role, in this instance's XTM Hub claim, that means administrator. Matched exactly, including case. **Setting it enables the gate**; empty leaves OIDC behaving exactly as it does without this feature. |

The gate reads a claim named after this instance — `{PLATFORM_ID}_groups` — so
`PLATFORM_ID` must be set when you enable it. It is the switch that is
`XTM_HUB_ADMIN_GROUP` rather than `PLATFORM_ID`, because forcing a platform
identifier is an ordinary thing to do for clone/restore identity continuity and
must not enable a sign-in gate as a side effect. Enabling the gate without
`PLATFORM_ID` **refuses to start**, rather than silently refusing every sign-in.

Enabling it changes three things at once:

- **Every** sign-in is checked, not only the first. Someone who already has an
  account and has since lost their Hub entitlement is turned away too.
- A refused sign-in **creates no account** and lands on the login page with a
  neutral message. The reason is in the server log, not on the page — the person
  is unauthenticated, and the criteria describe how your IdP is configured.
- Administrator follows the Hub in **both** directions: granted when the Hub
  asserts it, and removed when it stops. The account named by `ADMIN_EMAIL` is
  exempt from removal, so a Hub-side group change cannot lock you out of your own
  instance.

If a misconfigured gate refuses everyone, sign in with a local account — that
route stays available and is the way back in.

## MCP OAuth

MCP OAuth runs in two independent directions, and each has its own switch.

**Inbound** — an external MCP client (Claude Desktop, Cursor, …) connecting *to* XTM One. The platform acts as the authorization server and lets clients self-register, which the MCP specification requires because a client has no credentials before any user session exists.

**Outbound** — XTM One connecting *to* a remote MCP server. With no client ID configured, the platform discovers the remote authorization server and registers itself, so a server can be added from its URL alone. This is the MCP specification's intended onboarding path: modern remote MCP servers do not issue static credentials to paste into a form.

| Variable | Required | Default | Description |
|---|---|---|---|
| `MCP_DCR_ENABLED` | No | `true` | **Inbound.** Allow external MCP clients to self-register against this platform (`POST /mcp/oauth/register`, RFC 7591). The endpoint is unauthenticated by specification, so it is protected by a per-IP rate limit and a bounded client table rather than a login. Set `false` to turn registration off entirely (the endpoint then returns 403). A confidential client receives its `client_secret` in the answer, so under `CREDENTIAL_TRANSPORT_STRICT` a registration the platform cannot confirm arrived over HTTPS is refused first (`400 {"error": "invalid_client_metadata", "error_description": "https required"}`). Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `MCP_DCR_MAX_CLIENTS` | No | `500` | **Inbound.** Upper bound on dynamically registered clients. At the cap the oldest registrations are evicted; RFC 7591 clients re-register transparently. Values below 1 are clamped to 1. |
| `MCP_DCR_RATE_LIMIT` | No | `5` | **Inbound.** Registrations per hour from one client IP at `POST /mcp/oauth/register`. A hosted client (claude.ai, ChatGPT) registers one client per fresh connection, always from its provider's addresses: raise it when many people connect from such a client and it does not use a client metadata document. Values below 1 are clamped to 1. |
| `MCP_CIMD_ENABLED` | No | `true` | **Inbound.** Accept OAuth Client ID Metadata Documents (MCP 2025-11-25): a client names its own HTTPS metadata document as its `client_id` instead of registering, which is what claude.ai, Claude Code and VS Code use when the platform advertises it (`client_id_metadata_document_supported`). The platform fetches the document from a public address only (the outbound URL policy, connecting to the address it checked), with no redirect, within 5 seconds and 16 KiB in all, at most 20 fetches per client IP, 10 per host and 60 for the whole platform per minute (refused while Redis is unavailable), and caches it for its `Cache-Control` max-age (5 minutes to 1 hour, 1 hour by default); a known client is served from its cached entry for up to 24 hours more when a refetch cannot be made. Why a document was refused is logged, not shown to the caller. Only public clients with a `client_name` whose redirect URIs are HTTPS or loopback are accepted, and a person still signs in and consents on a page naming the client by its host. The refresh tokens of one authorization stop after 90 days, and sooner once its document is refused. Set `false` to refuse them: such a client then needs dynamic registration or an API key, and its tokens stop refreshing. Also in Settings > Access and security; the variable, when set, wins and locks the field. |
| `MCP_OUTBOUND_DCR_ENABLED` | No | `true` | **Outbound.** Let the platform register *itself* with a remote MCP server that advertises a registration endpoint, so remote servers can be connected without pre-provisioned credentials. Set `false` to require every outbound OAuth client to be provisioned by hand — connecting a remote server then needs an explicitly configured client ID. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |

## Code interpreter sandbox workspace

Every conversation and assignment run has a durable directory, `workspace/`, that the code interpreter keeps between calls (see [Working in the sandbox workspace](../usage/sandbox-workspace.md)). Its files are stored in the platform bucket (`S3_BUCKET`) under `sandbox-workspaces/<workspace id>/`, one object per distinct content, and its manifest in PostgreSQL; the sandbox receives only its own workspace's files and never a storage credential. No volume is needed: every replica and worker reads the same workspaces.

| Variable | Required | Default | Description |
|---|---|---|---|
| `CODE_INTERPRETER_WORKSPACE_ENABLED` | No | `true` | Keep a workspace per conversation and run, offer `session=true`, `to_workspace_path` and the `sandbox_*` / `materialize_repository` tools. `false` brings back the stateless code interpreter of earlier releases (no workspace, no tool or argument added). |
| `CODE_INTERPRETER_WORKSPACE_QUOTA_MB` | No | `2048` | Size limit of one workspace, in MiB. A call that would go past it saves nothing it wrote under `workspace/` and says so. |
| `CODE_INTERPRETER_WORKSPACE_MAX_FILES` | No | `20000` | Files one workspace may hold. |
| `CODE_INTERPRETER_WORKSPACE_IDLE_TTL_HOURS` | No | `168` | A workspace unused for this long is deleted by the hourly sweep. Archiving or deleting a conversation deletes its workspace at once. |
| `CODE_INTERPRETER_RUN_WORKSPACE_IDLE_TTL_HOURS` | No | `24` | An assignment run's workspace is deleted as soon as the run ends (completed, failed, skipped or cancelled). One whose run has not ended is deleted after this long unused, unless the run waits on a person (then `CODE_INTERPRETER_WORKSPACE_IDLE_TTL_HOURS` applies). |
| `CODE_INTERPRETER_WORKSPACE_MANIFEST_ENTRIES` | No | `40` | Files the `code_interpreter` description and the resume note list; the rest are counted. |
| `CODE_INTERPRETER_WORKSPACE_CACHE_MB` | No | `2048` | Local sandbox only: what each backend process keeps of the workspace contents it downloaded, under the system temporary directory, least recently used first out. `0` disables the cache (every call downloads what it mounts). |
| `CODE_INTERPRETER_KERNEL_IDLE_SECONDS` | No | `900` | A `session=true` kernel unused for this long stops (minimum 30). On OpenSandbox it is also the kernel container's lifetime, renewed by every call. |
| `CODE_INTERPRETER_KERNEL_MAX_PER_PROCESS` | No | `8` | Local sandbox only: kernels one backend process keeps, the least recently used stopped first. |
| `CODE_INTERPRETER_COMMAND_MAX_SECONDS` | No | `1800` | OpenSandbox only: the longest a check run by `sandbox_run_command` may take; a check's own `timeout` in `.xtm-one/checks.toml` is capped by it. The command's container is discarded after a timeout. |
| `CODE_INTERPRETER_COMMAND_ALLOW_ARGV` | No | `false` | OpenSandbox only: let `sandbox_run_command` run an `argv` the checkout did not declare in `.xtm-one/checks.toml`. Off: only declared checks run, and a refusal lists them. The container has no network and no credential either way. |
| `CODE_INTERPRETER_PACKAGE_ALLOWLIST` | No | empty | Packages `code_interpreter(packages=[...])` may install into a workspace, comma-separated distribution names. Empty: installs are off and the argument is not offered. The server installs wheels only, checked against the index's sha256, and runs no install script; a package the sandboxed code imports can import modules the sandbox blocks to the code itself, so list only packages you trust. The [security audit](../administration/settings.md#security-audit) reports a non-empty list. |
| `CODE_INTERPRETER_PACKAGE_INDEX_URL` | No | PyPI | The package index installs read: a PEP 691 or PEP 503 "simple" index, such as a PyPI mirror or proxy. |

**Storage sizing.** Plan for the bucket to hold, at most, the active conversations times the quota; in practice a workspace holds what an agent downloaded or wrote (a repository subset is usually a few MB), and an unused one goes after `CODE_INTERPRETER_WORKSPACE_IDLE_TTL_HOURS`. An assignment run's workspace goes when the run ends; after an upgrade, the first hourly sweeps delete the workspaces of the runs that ended before it (reason `run_ended` in the sweep's log line, with the bytes freed). Prometheus reports the total (`xtmone_sandbox_workspaces{measure="bytes"}`, see [Monitoring](monitoring.md#sandbox-workspace-metrics)), and the `get_platform_overview` tool reports `sandbox_workspaces` (count, bytes, by holder, the top owners and what the clean-up freed) next to `files.storage_bytes`. With the local sandbox, give each backend pod's temporary directory room for `CODE_INTERPRETER_WORKSPACE_CACHE_MB` plus the files of the calls running at once.

## OpenSandbox

Only used when `CUSTOM_TOOL_SANDBOX=opensandbox`. Governs the OpenSandbox-managed sandbox used by the code interpreter and by custom tools.

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENSANDBOX_DOMAIN` | Yes (if `opensandbox` mode) | `localhost:8090` | Host (and optional port) of the OpenSandbox service. `docker-compose.yml` and the dev override `docker-compose.dev.yml` default it to the `opensandbox` service (`opensandbox:8090`). The default isn't enforced at startup — a production deployment that forgets to set this won't fail loudly, it will just try to reach `localhost:8090` and fail to connect. |
| `OPENSANDBOX_PROTOCOL` | No | `http` | Protocol used to reach OpenSandbox (`http` or `https`). |
| `OPENSANDBOX_API_KEY` | Yes (with the `opensandbox` service) | — | API key for authenticating to OpenSandbox. `docker-compose.yml` gives the same value to the `opensandbox` service as its own key: unset, that server (v0.2.1 and later) refuses to start. The dev override `docker-compose.dev.yml` defaults it to `copilot-dev-opensandbox` on both sides. |
| `OPENSANDBOX_IMAGES` | No | `python:sandbox-python:0.4,nodejs:sandbox-nodejs:0.4` | Per-key container image, as comma-separated `key:image` pairs (split on the first `:` per entry, so a registry:port-qualified image survives intact). Overrides merge on top of the defaults, so overriding one key leaves the other on its default. |
| `OPENSANDBOX_K8S_POOL_NAMES` | No | — (cold start, k8s-only) | Per-key Kubernetes `Pool` binding, as comma-separated `key:pool-name` pairs, e.g. `python:my-python-pool,nodejs:my-js-pool`. A `Pool`'s warm pods run one fixed image, so one name can't back both sandbox kinds — a key missing from the mapping (or the whole variable unset) cold-starts for that key. |
| `OPENSANDBOX_K8S_NAMESPACE` | No | — (reaper off, k8s-only) | The namespace the OpenSandbox server creates its sandboxes and `Pool` pods in. Set, the worker reaps it every minute, and at once after a pooled create timed out: it deletes the `Pool` pods that ended (`Failed`, `Succeeded`, `Evicted`) or stayed not Running and ready past `OPENSANDBOX_POOL_STUCK_SECONDS`, which a `Pool` counts toward its warm buffer and never replaces, and the `BatchSandbox` objects past their `expireTime`. The platform's service account needs `pods` and `batchsandboxes.sandbox.opensandbox.io` get, list and delete in that namespace; without them, or outside a cluster, the reaper logs one line and does nothing. Deleted objects are counted by `xtmone_sandbox_pool_reaped_total{kind, reason}`. |
| `OPENSANDBOX_POOL_STUCK_SECONDS` | No | `600` | How long a `Pool` pod may stay Pending, Unknown or Running but not ready before the reaper deletes it (minimum 60). Keep it above the server's `sandbox_create_timeout_seconds`. |
| `OPENSANDBOX_NETWORK_ALLOW` | No | — (all egress denied) | Comma-separated list of domains/IPs to allow as egress from every OpenSandbox container. Empty means all egress is denied. In a JavaScript custom tool, a `fetch` to a host outside the list is refused at once with the egress-policy message, and a `fetch` to a listed host fails after 10 s when the host does not accept the connection. |
| `SANDBOX_REGISTRY_MIRROR_URL` | No | — (off) | URL of a read-only npm, PyPI and Maven Central mirror proxy for the containers `sandbox_run_command` and `sandbox_start_service` create (never the code interpreter or a custom tool), e.g. `http://registry-mirror:8080`. Set, its host is added to their egress allow-list and npm, Yarn, corepack, pip and uv install through it (`/npm/` and `/pypi/simple/` routes), and Maven through `/maven/` (a settings file mirroring every repository, `mirrorOf *`, named by `MAVEN_ARGS`). The shipped proxy is `backend/sandbox/registry-mirror/nginx.conf`: GET and HEAD only, no credential forwarded, any other path refused. Keep `registry.npmjs.org`, `pypi.org`, `files.pythonhosted.org` and `repo.maven.apache.org` out of `OPENSANDBOX_NETWORK_ALLOW` so the mirror is the only route. A URL with a user, a password, a query or a fragment is refused at startup. Empty: the dev images install offline from the caches they bake. |
| `SANDBOX_CPU` | No | `1` | CPU requested for each OpenSandbox container (code interpreter + custom tools). Passed through as-is to the OpenSandbox SDK's `resource` argument on the cold-start path. When `OPENSANDBOX_K8S_POOL_NAMES` binds that sandbox kind to a k8s `Pool`, it does not size the container — the `Pool` manifest's own `resources.requests` governs warm-pod sizing instead, so there's no sync requirement between the two - see `dev-docs/opensandbox-pool-management.md`. It also sets `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS` in every OpenSandbox container to this value rounded up to a whole CPU (at least 1), so numerical libraries such as numpy start one thread per allocated CPU, not one per host core. |
| `SANDBOX_RAM` | No | `128Mi` | Memory requested for each OpenSandbox container. Same cold-start-only pass-through as `SANDBOX_CPU`. Both take a bare value for every image key or a per-key list in the `OPENSANDBOX_IMAGES` convention (`128Mi,python-dev:2Gi`: the entry without a key is the default), so the dev images of `sandbox_run_command` (`python-dev`, `nodejs-dev`, built by `backend/sandbox/build-dev-image.sh` with the repository's locked dependencies) can be sized for a test suite without resizing every sandbox. The code interpreter and the custom Python tools read the `python` key, the custom JavaScript tools the `nodejs` key. |
| `OPENSANDBOX_USE_SERVER_PROXY` | No | `false` | Route sandbox traffic through the OpenSandbox server instead of connecting directly to each sandbox's endpoint. Useful for any deployment topology where this process can reach the OpenSandbox server but not the individual sandbox endpoints it hands back — e.g. a process on the host against a containerized server, which advertises endpoints via a hostname like `host.docker.internal` that only resolves from inside containers, or a production/k8s deployment where sandbox pods aren't directly routable from this process but the server's service is. |

### Running the OpenSandbox server itself

The variables above configure how the platform *connects to* an
OpenSandbox server. These configure how that server *itself* runs:

| Variable | Required | Default | Description |
|---|---|---|---|
| `CONTAINER_ENGINE_SOCKET` | No | `/var/run/docker.sock` | Container engine API socket mounted into the `opensandbox` service, which starts the sandbox containers through it. Under rootless Podman, `$XDG_RUNTIME_DIR/podman/podman.sock` (enable `podman.socket`). The service runs with SELinux labeling disabled (`security_opt: label=disable`), which an SELinux host needs for it to use the socket, and which leaves it unconfined by SELinux there; it has no effect on a host without SELinux. |

`docker-compose.yml` defines the OpenSandbox server, the `opensandbox` service, under the `opensandbox` profile: it starts only when you enable that profile (`docker compose --profile opensandbox up -d`, or `COMPOSE_PROFILES=opensandbox` in `.env`), and then needs `OPENSANDBOX_API_KEY`. It runs the sandboxes as Docker containers on the same host and answers at `opensandbox:8090`: set `CUSTOM_TOOL_SANDBOX=opensandbox` in `.env`. The platform and the workers read `CUSTOM_TOOL_SANDBOX` and the `OPENSANDBOX_*` connection variables from `.env`, and `OPENSANDBOX_DOMAIN` defaults to `opensandbox:8090`. The server advertises each sandbox as `host.docker.internal:<port>` (`backend/sandbox/sandbox.docker.toml`); the platform and worker services map that name to the host (`extra_hosts: host.docker.internal:host-gateway`), so they connect to the sandboxes directly and `OPENSANDBOX_USE_SERVER_PROXY` stays `false`. The dev override `docker-compose.dev.yml` always starts it. To run the sandboxes as pods on a Kubernetes cluster instead, point `OPENSANDBOX_DOMAIN` at an `opensandbox-server` deployed on that cluster; the cluster needs the BatchSandbox CRD, `opensandbox-controller` and the `kata-fc` RuntimeClass (see `dev-docs/opensandbox-pool-management.md`).

- **Target namespace on Kubernetes**: set via the `namespace` key in `[kubernetes]` of the server's configuration file (`backend/sandbox/sandbox.k8s.firecracker.toml` is a starting point) — there's no env var for this specific setting, it's TOML-only. (`opensandbox-server` itself does read two env vars: `OPENSANDBOX_SERVER_API_KEY` for its API key, and `SANDBOX_CONFIG_PATH` as a fallback for `--config` when no CLI flag is given — but neither substitutes for TOML settings like `namespace`.) Defaults to `null` (server default) if omitted. See the [OpenSandbox server configuration reference](https://github.com/opensandbox-group/OpenSandbox/blob/main/server/configuration.md#kubernetes--only-when-runtime--kubernetes) for the full `[kubernetes]` section.

### Sandbox call log fields

Every OpenSandbox-routed call (code interpreter, custom JS tool, custom
Python tool) emits one structured `sandbox_call` log event to stdout
(JSON in production — see [Logging](#logging) above), independent of the
`quota_usage` counters. These fields are stable
across releases so a log pipeline (e.g. shipping stdout into Grafana/Loki)
can filter and aggregate on them; a field not listed here (e.g. `level`,
`timestamp`, `logger`) is generic structlog metadata, not part of this
schema.

| Field | Type | Present when | Description |
|---|---|---|---|
| `usage_type` | string | Always | One of `code_interpreter`, `custom_tool_js`, `custom_tool_python`. |
| `user_id` | string (UUID) | Only when the call had a known user context | The caller's user ID. |
| `outcome` | string | Always | `success` or `failure`. |
| `failure_reason` | string | Only when `outcome=failure` | One of `sandbox_ready_timeout` (the sandbox never became ready in time), `sandbox_create_failed` (other sandbox-creation error), `sandbox_create_error` (unexpected error creating the sandbox), `nonzero_exit` (the executed code/tool exited with a non-zero status), `execution_error` (an unexpected error while running the code/tool). |
| `cpu_used_percentage` | float | Successful calls and `nonzero_exit` failures, when metrics could be retrieved | CPU used, as a percentage. |
| `cpu_count` | float | Same as `cpu_used_percentage` | Number of CPU cores allocated to the sandbox. |
| `memory_used_in_mib` | float | Same as `cpu_used_percentage` | Memory used, in MiB. |
| `memory_total_in_mib` | float | Same as `cpu_used_percentage` | Memory allocated to the sandbox, in MiB. |
| `tte_ms` | integer | Successful calls and `nonzero_exit` failures, when the sandbox backend reported a completed execution | Time-to-execute: the sandbox's own reported execution duration, in milliseconds (not overall call time, which also includes sandbox creation/setup). Can be absent even on a `nonzero_exit` failure if the process errored before an execution-complete event was ever emitted (e.g. an immediate crash). |
| `egress_default_action` | string | Always | Always `"deny"` today. |
| `egress_allow_list` | array of strings | Always (may be empty) | The egress allow-list (`OPENSANDBOX_NETWORK_ALLOW`) applied when the sandbox was created. |

CPU/RAM/TTE are omitted (not emitted as `null`) whenever they aren't
available — e.g. no sandbox was ever created (`sandbox_ready_timeout`,
`sandbox_create_failed`, `sandbox_create_error`), or the metrics lookup
itself failed or timed out. A slow or unreachable metrics lookup is
bounded to a few seconds and never delays or fails the underlying sandbox
call.

---

## Webhook delivery

Governs `POST /api/hooks/assignments/{assignment_id}` — the endpoint an external system (SIEM/SOAR, ticketing, CI) posts to in order to drive an agent.

A delivery that finds its assignment busy is **queued**, not discarded: it becomes a run in the `queued` status, and an accepted delivery produces exactly one run, eventually. Queued runs start the moment they are queued and whenever a run ends, up to `ASSIGNMENT_MAX_CONCURRENT_RUNS` runs of one assignment at once, in turns across agents and assignments so one busy assignment cannot starve the others. Runs that must not overlap - the same ticket, the same thread - are kept one at a time by an assignment's `ordering_key` (below). Schedules, manual triggers and follow-ups keep their own overlap guard: they never start beside an active run.

| Variable | Required | Default | Description |
|---|---|---|---|
| `ASSIGNMENT_QUEUE_ENABLED` | No | `true` | Per-assignment webhook (`POST /api/hooks/assignments/{id}`) only: queue its deliveries that arrive while the assignment is busy. Integration events follow `INTEGRATION_EVENT_QUEUE_ENABLED` instead. Set `false` only to restore the legacy behaviour, where such a delivery was **discarded** and still answered `200`. |
| `INTEGRATION_EVENT_QUEUE_ENABLED` | No | `false` | Pushed integration events - a Slack event, a Microsoft Graph or LinkedIn notification, a provider or agent webhook - that arrive while every slot of their assignment is taken. `false`: the overlap guard skips them, as it always did (a Slack message is still fetched by the poller). `true`: they are queued, past the queue cap with a warning if need be, and start as slots free. Polled events (email, Teams and Slack messages) are always queued, whatever this says - see below. |
| `ASSIGNMENT_EVENT_DEDUP_WINDOW_SECONDS` | No | `600` | One run per inbound event on an agent. A second event-triggered run with the same dedup key (a mail's Message-ID, a calendar event and its modification instant, a provider event id) on the same agent within this many seconds is recorded as a skipped run (`skipped: duplicate of run <id>`, counted in the assignment's filtered events) instead of starting, across the agent's assignments and across delivery paths (a Graph notification and the poll behind it, two mailboxes the mail reached, a delegated pick-up naming the Message-ID). The same window covers an agent account of the same agent on a mailbox the mail reached: whichever of the assignment and the agent account takes the mail first answers it, the other leaves it (the assignment run is recorded as `skipped: duplicate of run the agent account <id>`, the agent account's activity as `already_handled`). An assignment's `trigger_config.dedup_window_seconds` overrides it; `0` turns the dedup off for it. Scheduled runs are not affected. |
| `MAIL_SEND_GUARD_WINDOW_SECONDS` | No | `300` | One answer per conversation from a mailbox an agent sends through, the last line behind the dedup above. Before a Microsoft mail tool sends (`send_microsoft_email`, `send_microsoft_draft`, a reply draft sent, `reply_microsoft_email_in_thread` with `send=true`), it takes a short lock on the conversation for the sending mailbox and looks for a message that mailbox (any agent, any run) already sent in that conversation within this many seconds: its own record of the last send, then the mailbox's Sent Items. Found, the send is refused and the tool answer names the earlier message (when it was sent, its subject, its recipients, its id and link); the agent can pass `force_duplicate=true` when a second message is really wanted. A message from someone else that arrived in the conversation after the earlier send lets the new send go out (it answers that message); an automatic reply (an out-of-office answer, told by its `Auto-Submitted` / `X-Autoreply` / `Precedence: auto_reply` headers or its "Automatic reply:" subject) does not. A new email's conversation is its subject (reply and forward prefixes dropped) and its To / Cc addresses. `0` turns the guard off. |
| `INTEGRATION_EVENT_GRAPH_SUBSCRIPTIONS_ENABLED` | No | `true` | Deliver Microsoft integration-event assignments (new email, Teams messages, replies and chat messages, calendar changes, email flag changes) through Microsoft Graph change notifications posted to `BASE_URL/api/v1/integrations/notifications/microsoft`, within seconds instead of on the 5-minute poll. Only effective when `BASE_URL` is a public `https` URL (not `localhost`, a private address or a `.local` / `.internal` / `.lan` host). The scheduler creates each subscription with a random `clientState`, renews it before it expires and deletes it when the assignment no longer needs it; an assignment whose subscription cannot be created, or lapses, is polled as before, and picks up from the last notified event. `false` deletes the subscriptions on the next pass and polls everything. |
| `ASSIGNMENT_MAX_CONCURRENT_RUNS` | No | `10` | Runs of one assignment started from its queue at once (webhook deliveries, polled and pushed integration events), clamped to 1-50. An assignment's `trigger_config.max_concurrent_runs` overrides it. To keep up, the cap must exceed deliveries per second times the seconds a run holds its slot: 30 deliveries a minute of 6-22 s runs keep 8 in flight on average. `1` restores one run at a time. |
| `ASSIGNMENT_DISPATCH_MAX_PENDING_RUNS` | No | `0` | Queued runs promoted and still waiting for a worker, across the platform. The drain stops there, so a promoted run starts within a reconciler cycle and one busy assignment cannot fill the worker pool ahead of the others. `0` = `MAX_CONCURRENT_RUNS`. |
| `ASSIGNMENT_FAIR_SHARE_ENABLED` | No | `true` | Share the worker pool between fleets when a free slot claims the next run: a delegated sub-agent run whose parent is still running goes first, then one run per fleet in turn (an agent outside any fleet counts as its own fleet), oldest first within a fleet. A storm of one fleet's events then delays another fleet's scheduled or event run by one slot release at most. `false` restores the strict oldest-first claim. |
| `ASSIGNMENT_FLEET_MAX_SHARE` | No | `0.5` | The share of the pool (`MAX_CONCURRENT_RUNS` times the workers) one fleet may hold while another fleet has runs waiting; past it, its runs go after the others'. Nothing is refused: an otherwise idle pool still runs them. `0` or `1` lifts the cap. |
| `ASSIGNMENT_AGENT_MAX_SHARE` | No | `0.5` | The same share for one agent, inside or outside a fleet. `0` or `1` lifts the cap. |
| `DELEGATION_RESERVE_SLOTS` | No | `2` | Slots per worker above the shared pool that only a delegated child (an assignment child run or a background task) whose parent run is still running may take, once the pool is full: parents waiting on their own children never hold the pool until a deadline. `0` turns the reserve off. |
| `ASSIGNMENT_QUEUE_BACKLOG_SECONDS` | No | `300` | Once an assignment's oldest queued delivery has waited this many seconds, the drain alternates its newest and oldest deliveries, so fresh work does not wait behind the whole backlog (nothing is dropped), and logs `Assignment dispatch lag` at `WARNING` at most once a minute. `0` disables both. |
| `ASSIGNMENT_DISPATCH_RATE_LIMIT_BACKOFF_SECONDS` | No | `60` | How long an assignment's concurrency cap stays halved after one of its runs met an HTTP 429, from a model provider or an integration. Only that assignment backs off; a 429 met anywhere else (a chat, an evaluation, an embedding) throttles nothing. The window starts at the first 429 and is not extended by the ones that follow. The providers' own retries are unchanged. `0` disables the back-off. |
| `ORCHESTRATION_MAX_QUEUE_WAIT_SECONDS` | No | `3600` | How long an assignment run or a background task may wait for a free worker before it fails with the cause `deadline_exceeded_before_start` (nothing runs, nothing is charged, no `run.failed` event). A run's `timeout_seconds` is its execution time and counts from the moment a worker starts it, never from its creation, so time spent in the queue is not taken from it. `0` removes the queue bound. An assignment's `trigger_config.max_queue_wait_seconds` overrides it. |
| `ASSIGNMENT_QUEUE_MAX_DEPTH` | No | `20` | Maximum deliveries held per assignment. Bounds the backlog a slow or stuck assignment can accumulate. |
| `ASSIGNMENT_QUEUE_OVERFLOW_POLICY` | No | `reject` | Per-assignment webhook only: what happens to one of its deliveries arriving at a full queue (a queued integration event is never dropped). `reject`: HTTP 429 + `Retry-After`, so a sender with retry logic loses nothing. `drop_oldest`: the oldest waiting delivery is finalized as a `skipped` run (visible, payload preserved) and the new one takes its slot. `drop_newest`: the incoming delivery is discarded with a `200` and a warning — no run row is created. |
| `ASSIGNMENT_QUEUE_RETRY_AFTER_SECONDS` | No | `30` | `Retry-After` value returned with the `reject` policy's 429. |
| `ASSIGNMENT_QUEUE_HOLD_ON_AWAITING_HUMAN` | No | `true` | Whether a run paused for human input holds the queue. `true` starts nothing more for its assignment until the approval is answered, whatever the cap - bounded and visible, unlike the old silent drop. `false` lets queued runs start beside a paused run; only safe when a HITL pause leaves no half-written shared state. |
| `ASSIGNMENT_QUEUE_DRAIN_BATCH` | No | `20` | Maximum assignments that start queued runs per drain. Only assignments with a free slot are counted: one whose slots are all taken keeps its backlog without taking a turn. The drain runs on every reconciler cycle, on every enqueue and at the end of every run, so the rest follow within a cycle. |
| `WEBHOOK_REQUESTS_PER_MINUTE` | No | `60` | Per-assignment delivery throttle, applied before the request body is read. Over-limit deliveries get 429 + `Retry-After`. Raise it for a high-cadence alert feed — the queue still bounds how many accepted deliveries become runs. |
| `WEBHOOK_EXACT_EVENT_MATCHING` | No | `false` | Event matching of integration-event and webhook triggers. A trigger created on this version, or whose events are chosen again, matches its event exactly: `pull_request` runs on `pull_request.opened`, never on `pull_request_review.submitted`. A trigger saved on an earlier version keeps its prefix match through every other edit and every copy (duplicate, import, catalog clone); its assignment page says which mode it uses and what it matches, with a one-click switch, and the first delivery it matches only by prefix is logged. `true` applies exact matching to every trigger without editing them; it does not read the old `event_type` key of an agent-builder trigger, which only that trigger's own switch does. Also in **Settings > Access and security** ("Exact event matching for every trigger"); the variable, when set, wins and locks the field. See [Event matching](../usage/webhook-triggers.md#event-matching). |
| `WEBHOOK_DEDUP_TTL_SECONDS` | No | `300` | Idempotency window. A delivery whose key was already seen within the window replays the first delivery's response instead of firing a second run. |
| `CHAT_EVENT_COALESCE_WINDOW_SECONDS` | No | `6` | One turn per chat conversation (a Teams 1:1 or group chat, a Teams channel thread, a Slack DM, channel or thread). The run of a message starts this many seconds late; a later message of the same sender in the same conversation that finds it still waiting joins its turn (one run, one reply, the messages in order and flagged as merged) instead of starting a second run. An assignment's `trigger_config.coalesce_window_seconds` overrides it (0-60); `0` turns the window off. |

`ASSIGNMENT_QUEUE_ENABLED`, `ASSIGNMENT_QUEUE_OVERFLOW_POLICY` and `ASSIGNMENT_QUEUE_RETRY_AFTER_SECONDS` govern the per-assignment webhook (`POST /api/hooks/assignments/{id}`) only; the concurrency, depth, hold, backlog and back-off settings apply to every queued delivery.

A polled email, Teams or Slack message is always queued. Each poll reads its provider's events oldest first, up to 5 pages - 15 Microsoft emails, 10 Gmail messages, 20 Teams or Slack messages a page - and queues them, so a backlog drains as fast as the assignment's slots allow instead of one event per poll. When the queue is full, the rest is given back to the poller with its position and fetched again on a later tick; nothing is dropped. A backlog larger than one poll is logged once at `INFO` level. What a poll cannot fetch is logged as a warning: more than 250 new messages in one Teams channel or 1,000 in one Slack channel, more than 100 unread Gmail messages, more than a page of new Teams, Slack or Gmail events sharing one timestamp, or more than 1,000 Microsoft emails received within one second (a second holding more emails than a page is read whole before the poll moves past it). Events that arrived while the platform was down stay with their provider and are read the same way once it is back: the poll resumes from its saved position (kept 30 days) and skips events it already dispatched (kept 7 days).

A pushed event - a Slack event, a Microsoft Graph or LinkedIn notification, a provider or agent webhook - starts in a free slot. With every slot taken, it is skipped unless `INTEGRATION_EVENT_QUEUE_ENABLED` is `true`, in which case it is queued past the cap with a warning in the log.

An assignment's trigger configuration can refine the queue:

| `trigger_config` key | Effect |
|---|---|
| `max_concurrent_runs` | This assignment's cap, overriding `ASSIGNMENT_MAX_CONCURRENT_RUNS` (1-50). `1` runs its deliveries one at a time. |
| `max_queue_wait_seconds` | How long one of its runs may wait for a free worker, overriding `ORCHESTRATION_MAX_QUEUE_WAIT_SECONDS` (`0`: no bound). Past it the run fails as `deadline_exceeded_before_start`, with `trigger_context.queue.queue_wait_seconds` recorded. |
| `ordering_key` | A dotted path into the trigger payload, such as `issue.number`. Runs whose payload has the same value there start one at a time, in arrival order; runs with different values, or none, run side by side. Mail triggers are ordered by thread without it (Microsoft `conversation_id`, Gmail `thread_id`), and chat messages by their conversation (`conversation_key`: a Teams chat, a Teams channel thread, a Slack DM or channel, a Slack thread); `""` turns that off. |
| `coalesce_window_seconds` | The coalescing window of this assignment's chat messages, overriding `CHAT_EVENT_COALESCE_WINDOW_SECONDS` (0-60). |
| `batch_window_seconds` | `integration_event` and `platform_event` triggers only (0-600, default `0`: one run per event). The first event opens a batch run that starts once the window ends; every event arriving while it waits joins it. The run's payload carries `batch: true`, `event_count` and `events`, the list in arrival order (at most 100 per batch, the next event opens a new one). Use it for observers of `run.*` events. |
| `preload_tool_groups` | Tool groups (ids such as `integration:github`, at most 6) every run of this assignment loads before its first request, so it never calls `load_tools` for them. A tool group loaded mid-run changes the tool list at the head of the request and invalidates the model provider's prompt cache for the rest of the run; a preloaded group keeps it valid. A group the agent does not hold is ignored. Any trigger type. |

A chat message is never dropped for a busy assignment, whatever `INTEGRATION_EVENT_QUEUE_ENABLED` says: a message arriving while a run of its conversation is in flight is queued and starts when that run ends, so it is answered once, after the first reply. Messages of the same sender sent within the window are merged into one turn: the run's payload carries `messages` (in order), `message_ids`, the joined `text`, `coalesced_count` and a `coalesced_note` asking the agent to answer once.

A run that is not a delivery - a manual trigger, a follow-up, a scheduled or chained run - has its assignment to itself: deliveries wait until it ends, as they did before parallel runs. A delivery that cannot be queued (a pushed event with `INTEGRATION_EVENT_QUEUE_ENABLED=false`, the per-assignment webhook with `ASSIGNMENT_QUEUE_ENABLED=false`) never starts ahead of a queued run of its own `ordering_key`: it is skipped instead. One without a key may start in a free slot ahead of the queue.

The worker logs one line per drain (`Assignment dispatch: promoted ...`) and `/metrics` serves, per assignment (labelled by `assignment_id`, never by its name), `xtmone_assignment_queue_depth`, `xtmone_assignment_queue_oldest_age_seconds`, `xtmone_assignment_runs_active`, `xtmone_assignment_concurrency_cap`, `xtmone_assignment_queue_held` and `xtmone_assignment_dispatch_lag_p95_seconds` (created-to-started p95 over the last 15 minutes), plus `xtmone_assignment_queue_oldest_age_max_seconds` across assignments. `k8s/prometheus-rules/assignment-dispatch-alerts.yaml` alerts when a delivery has waited more than 5 minutes.

### Response statuses

| `status` | Meaning |
|---|---|
| `ok` | A run started. `triggered[0].run_id` is its id. |
| `queued` | Accepted, waiting behind the active run. `run_id` is the queued run, `queue_depth` the current backlog. |
| `duplicate` (with `deduplicated: true`) | The same idempotency key already arrived within the window. The original response is replayed; nothing new was triggered. |
| `dropped` | The queue was full under `drop_newest`. **This delivery will never run.** |
| `ignored` | The target does not resolve (unknown, disabled, or not a webhook trigger), or an event filter excluded it. |
| HTTP 429 | Not accepted — retry after the `Retry-After` delay. Either the per-assignment throttle or a full queue under `reject`. |

### Idempotency

Duplicate deliveries — proxy retries, sender at-least-once redelivery, a double-submitted test POST — are deduplicated so a retry returns the original `run_id` without re-running the assignment. The key is derived in this order:

1. An explicit header: `Idempotency-Key`, `X-Idempotency-Key`, `X-GitHub-Delivery`, `Webhook-Id` / `Svix-Id`, or `X-Hook-Signature-Id`.
2. A provider-specific id in the payload (OpenCTI `notification_id`, Slack `event_id`, HubSpot `eventId`, or a JSON-API `type` + `id` pair).
3. A SHA-256 of the URL scope + raw request body.

!!! tip "Send an `Idempotency-Key`"
    Step 3 cannot tell a retry apart from a genuine recurrence. Two distinct events carrying byte-identical bodies inside the window — a repeated detection with no timestamp, a state notification with no counter — collapse into a single run, and the second delivery is answered with `deduplicated: true`. Sending an `Idempotency-Key` that is unique per logical event removes the ambiguity entirely. Failing that, shorten `WEBHOOK_DEDUP_TTL_SECONDS` below the sender's event cadence.

---

## Assignment auto-pause

A scheduled assignment whose configuration points at a resource its integration can no longer see - a Notion database that was deleted or is no longer shared with the integration - would otherwise fail on every run, forever. Once the integration has reported the same resource missing in two separate runs, the run that uses it again is recorded as **failed** with the cause `Integration resource missing` (its summary is kept), and the assignment owner is notified once. After the number of consecutive scheduled runs below, the assignment is **paused**; the assignment page says which resource and integration are involved and offers `Resume`. See [Operations](../usage/work-history.md#when-an-integration-resource-goes-missing).

| Variable | Required | Default | Description |
|---|---|---|---|
| `ASSIGNMENT_STALE_REFERENCE_PAUSE_AFTER` | No | `3` | Consecutive scheduled runs failed on a stale integration reference after which the assignment is paused. Re-enabling the assignment starts a fresh count; a scheduled run that succeeds or fails for another reason resets it; manual, webhook and event-triggered runs neither advance nor reset it. Built-in and per-user fan-out assignments are reported, never paused. `0` disables the pause (runs still fail and are still reported). |

---

## CTEM case owner

A CTEM case opened from a signal starts with an owner: the owner of an affected asset in the inventory, else the owner of an asset group that asset belongs to, else the default below. An owner counts only when it is the email of an active account; any other name is skipped, never guessed. Each assignment is written in the case history with how the owner was found.

| Variable | Required | Default | Description |
|---|---|---|---|
| `CTEM_CASE_DEFAULT_OWNER` | No | (empty) | Email of the account a new case is assigned to when no affected asset or asset group names an owner with an account. Empty: the case opens without an owner. |

---

## Messaging channels

| Variable | Required | Default | Description |
|---|---|---|---|
| `CHANNEL_AGENT_MENTIONS_ENABLED` | No | `false` | On Slack, Teams, WhatsApp, Telegram, Discord and Google Chat, let an identified user's `@agent-key` (an agent's slug, else its hyphenated name) make that agent answer after the routed one, in the same reply - only an agent whose own channel settings let it answer that message (enabled, set to **Receive** and **Publish** on that channel, past the bot's receive whitelist, private-conversation and only-mention rules). Each agent that answers counts as one agentic action. `false`: a channel message is answered by its routed agent alone, whatever it mentions. The web chat's mentions are not affected by this setting. |
| `DECISIONS_READ_ENABLED` | No | `false` | Human gates: let the inbox, the bell and the decision cards read the unified decisions store. It takes effect only once every open question (a paused run, a pending tool approval, a flow waiting on a person, a work item waiting for an answer) has its decision: `GET /api/v1/decisions/backfill` (administrators) shows the count per source and whether the switch is on. `false`: they read each engine's own state, which stays current either way. |

## Built-in web search

One web search provider is given to every agent without an integration to add. Brave Search wins when its key is set; Google Search is the fallback; with neither key set, agents have no built-in web search.

| Variable | Required | Default | Description |
|---|---|---|---|
| `BUILTIN_BRAVE_SEARCH_API_KEY` | No | empty | Brave Search API subscription token, created in the Brave Search API dashboard. When set, the built-in **Brave Search** integration gives every agent web, news, image and video search, and Google Search below is not seeded. Its web and image tools also answer to the Google tool names (`search_google_web`, `search_google_images`), so agent instructions that name them keep working. |
| `BUILTIN_GOOGLE_SEARCH_API_KEY` | No | empty | Google Custom Search, as `search_engine_id\|\|api_key`. Used only when `BUILTIN_BRAVE_SEARCH_API_KEY` is empty. Google's Custom Search JSON API is closed to new customers and stops on 1 January 2027: move to Brave Search before then. |

## Microsoft 365 integration

| Variable | Required | Default | Description |
|---|---|---|---|
| `MICROSOFT_TEAMS_MESSAGE_TEXT_LIMIT` | No | `4000` | The longest text of one message an agent's Teams channel or chat listing shows. A longer message is cut at a word boundary and ends with a `[truncated, ...]` note giving how many characters are left and the exact call that reads it in full (`get_microsoft_channel_message` or `get_microsoft_chat_message`); a message that fits is shown whole. The messages of one listing also share a text budget of 40,000 characters, so on a busy channel the longest messages are cut shorter while the short ones stay whole. A value below `1` fails startup. |

---

## Runners and XTM One Desktop

A Runner is the pull-based native daemon behind **XTM One Desktop** (the native client users install on their own machine - chat window plus local execution) and behind the **headless runner** installed on servers and fleets. A user enrolls it once from the **XTM One Desktop** page (bottom of the left navigation) and leaves it running. It makes only outbound connections and **pulls** commands from the platform (the platform never opens a connection to the machine), giving agents genuine full-desktop screenshots, real-browser control, shell + file operations, and synthetic input through the `computer_use` built-in tool. Enrollment is a single-use, short-lived token exchanged once for a durable credential stored in the machine's OS keychain. Single sign-on from the Desktop app runs in the system browser and hands the session back through the runner-authenticated `/api/v1/desktop-login` handoff (short-lived Redis records; no extra configuration).

The installers, bare-binary archives, and self-update artifacts are **built by the release pipeline and embedded in the platform image** — every instance serves its own version-matched downloads directly from the **XTM One Desktop** page and answers the runners' self-update checks from the same embedded set. There is no external download URL, CDN, or GitHub Releases dependency to configure, and an air-gapped deployment is fully self-sufficient. A source/dev build (no embedded distribution) simply reports downloads as unavailable while everything else — enrollment, transport, `computer_use` — keeps working with runners you built yourself (`apps/runner/build.ps1` / `build.sh`).

| Variable | Required | Default | Description |
|---|---|---|---|
| `RUNNERS_ENABLED` | No | `true` | Master switch for the whole Runner subsystem: enrollment, the runner transport (WebSocket + long-poll), and the `computer_use` agent tools. Set `false` to remove it entirely (every runner endpoint then returns 404 and the Profile tab is hidden). |
| `RUNNER_DIST_DIR` | No | `runner_dist` | Directory of the embedded runner distribution inside the image, populated by CI at image build time. Only needs changing if you repackage the image and relocate the artifacts. Empty/absent (source builds) reports downloads as unavailable. |
| `RUNNER_MIN_SUPPORTED_VERSION` | No | — | Minimum runner binary version accepted at connect (protocol gate). Empty means no floor; a runner below it is told to self-update. Format `MAJOR.YYMMDD.SEQ`. |
| `RUNNER_MAX_PER_USER` | No | `200` | Cap on active runners a single user (or the company-managed fleet) may have enrolled at once, enforced at enrollment so an unattended roll-out cannot create unbounded records. `0` = unlimited. |
| `RUNNER_MAX_INFLIGHT_COMMANDS` | No | `10` | Cap on concurrent in-flight commands per runner; a burst past this is refused with a transient "busy" so parallel tool calls cannot swamp one machine. `0` = unlimited. |
| `RUNNER_STRICT_PATH_CONFINEMENT` | No | `false` | What a restricted runner does with a file command whose path lies inside its allowed folders but leads outside them through a link (symbolic link, junction, mount point). `false`: it runs, as it always has, with a warning in the command result and the runner log; the [security audit](../administration/settings.md#security-audit) (`Settings` → `Security`) reports the default while a restricted runner has allowed folders. `true`: it is refused. The same switch covers a file that is not a regular file - a device (`/dev/urandom`, a serial port, `NUL` on Windows), a named pipe or a socket, as the operating system reports the object the command opened, never guessed from its name - on every runner, full control included: `false` reads and writes it as before, with a warning in the result and the runner log; `true` refuses it. Needs a runner from this release or later; an older runner ignores it. Also in **Settings > Access and security**; the variable, when set, wins and locks the field. |
| `RUNNER_RESULT_WAIT_TIMEOUT_SECONDS` | No | — (derived) | How long an agent's `computer_use` call waits for a command's result. Unset: as long as the machine may take to report it, counted from the command's delivery - its execution limit (`RUNNER_COMMAND_DEFAULT_TIMEOUT_SECONDS` + 30 s, the wait for its turn on the machine included), then the 50 s it retries a result upload through a platform restart, plus 5 s. That is 205 s with the default limit, and it ends before the command's lease, so a command the wait gives up on is never sent to the machine again. A number: a fixed wait of that many seconds from dispatch. Either way a call never waits past the time the agent run allows one tool call (300 s at most): when that limit comes first, the tool stops 5 s before it and tells the agent the command may still finish, its output then landing in the machine's command history. **Upgrading:** the default used to be a fixed 180 s, which could end before a command near its limit whose upload rode out a restart had reported (see `UPGRADING.md`); set `180` to keep it. |

!!! note "Tuning knobs"
    The transport cadence and timeouts (`RUNNER_HEARTBEAT_INTERVAL_SECONDS`, `RUNNER_OFFLINE_AFTER_SECONDS`, `RUNNER_LONG_POLL_TIMEOUT_SECONDS`, `RUNNER_COMMAND_DEFAULT_TIMEOUT_SECONDS`, `RUNNER_MAX_RESULT_BYTES`, `RUNNER_ENROLLMENT_TTL_SECONDS`) have sensible defaults and rarely need changing; `.env.sample` lists each with its default. `RUNNER_COMMAND_DEFAULT_TIMEOUT_SECONDS` (120 s) is the execution budget every command is handed: the machine stops a command at that budget + 30 s, the wait for its turn included (a `run_shell` `timeout` argument can only shorten it), and both the time before the platform sends a silent command again and the default `RUNNER_RESULT_WAIT_TIMEOUT_SECONDS` follow it.

## Outbound proxy and certificate authorities

The requests that fetch a web page or document at a URL a user, an agent or a remote page chose - the `web_fetch` tool (agents and flows), the knowledge base URL crawl, Deep Investigation's web pages and the free HTTP sources of a built investigation pack - are sent to the address the destination policy checked, never to the name again, so a DNS answer that changes in between cannot point them at an internal address. Two consequences for the network around the platform:

- **They never go through an outbound proxy.** `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` are ignored for them (a proxy would resolve the name itself, after the check), and no relay group carries them. A deployment that reaches the web only through a proxy must give the platform and the worker direct outbound access to ports 80 and 443 for these requests; to keep filtering them, do it on the network path (an egress firewall, or a transparent proxy that applies its own destination policy) rather than through the proxy variables. When one of these variables is set, the platform and the worker each log one warning at startup naming the variable (never its value). How other outbound requests treat the proxy variables is unchanged (for MCP servers, see `MCP_SERVER_URL_POLICY` above).
- **They verify certificates against `SSL_CERT_FILE` / `SSL_CERT_DIR` when set**, the certifi bundle of public authorities otherwise. Set one of them for a private CA or a TLS-inspecting egress gateway. The file or directory **replaces** the default bundle for these requests: a file holding only your private CA makes every public site fail its certificate check, so append your CA to a copy of a public bundle. Set the variable, and mount the file, in both the platform and the worker containers (with Docker Compose, in both services' `environment`).
- **An administrator can add certificates without replacing the bundle.** Settings > Access and security > Security options > **Trusted certificates for web fetches** (`WEB_FETCH_TRUSTED_CA_CERTIFICATES`, which wins and locks the field) takes one or more PEM certificates - a private certificate authority, or the self-signed certificate of one host - and adds them to the store above for these requests. Verification stays on: the chain must verify to a listed or system certificate and the host name must match the certificate, so a listed self-signed certificate vouches for the host it names and no other. Nothing ever fetches with verification off. The value is checked when it is saved (certificates only; a private key is refused), applies to the next fetch on every replica within seconds, is audited (the audit log and the Security audit show how many certificates, never their text), and is empty by default. These fetches still refuse private, loopback and link-local addresses: a certificate makes a public-facing host trusted, it does not open the internal network.

## Relays

A relay is a small, separate program you install inside a network the platform cannot reach - a customer data center, an isolated zone - so the integrations, MCP servers and flow HTTP requests you route through it can call the tools there (Splunk, Tanium, an internal API). It opens an **outbound** connection to the platform and pulls the requests to carry: no inbound flow is ever opened toward that network. Relays are grouped by zone; every relay of a group carries the same traffic, so adding a second one gives the zone high availability. See [Relays](../usage/resources/relays.md) for enrollment and routing.

The installers (Linux, macOS, Windows), the archives and the self-update artifacts are **built by the release pipeline and embedded in the platform image**, exactly like the runner's: every instance serves its own version-matched downloads and answers its relays' update checks, and an air-gapped deployment needs nothing else. A container image (`xtmone/relay`) and a Kubernetes example are published for container platforms.

| Variable | Required | Default | Description |
|---|---|---|---|
| `RELAYS_ENABLED` | No | `true` | Master switch for relays: relay groups, enrollment, the relay transport (WebSocket + long-poll) and routing an integration, an MCP server or a flow HTTP node through a group. Set `false` to remove them entirely: every relay endpoint returns 404, the `Relays` page is hidden and nothing is routed - a resource already routed through a group then fails its requests rather than calling directly. |
| `RELAY_MIN_SUPPORTED_VERSION` | No | — | Minimum relay version accepted at connect. Empty means no floor; a relay below it is refused and told to update. Format `MAJOR.YYMMDD.SEQ`. The [security audit](../administration/settings.md#security-audit) lists the relays older than the floor. |
| `RELAY_MAX_PER_GROUP` | No | `50` | Cap on relays enrolled in one group, enforced at enrollment. `0` = unlimited. |
| `RELAY_REQUEST_LOG_RETENTION_DAYS` | No | `30` | Days the platform keeps the record of each relayed request (destination host, method, status, size, duration - never a body or a header value), shown on a group's `Activity` tab. `0` = forever. |
| `RELAY_DIST_DIR` | No | `relay_dist` | Directory of the embedded relay distribution inside the image, populated by CI at image build time. Empty/absent (source builds) reports downloads as unavailable; relays you built yourself still enroll and carry traffic. |

!!! note "Tuning knobs"
    The transport cadence, timeouts and size limits (`RELAY_HEARTBEAT_INTERVAL_SECONDS`, `RELAY_OFFLINE_AFTER_SECONDS`, `RELAY_LONG_POLL_TIMEOUT_SECONDS`, `RELAY_QUEUE_TIMEOUT_SECONDS`, `RELAY_STREAM_IDLE_TIMEOUT_SECONDS`, `RELAY_MAX_REQUEST_BYTES`, `RELAY_MAX_RESPONSE_BYTES`, `RELAY_ENROLLMENT_TTL_SECONDS`) have sensible defaults and rarely need changing; `.env.sample` lists each with its default. A request routed to a group with no relay online fails at once with a clear error - it never falls back to a direct call.

## Feature flags

| Variable | Required | Default | Description |
|---|---|---|---|
| `ENABLED_FEATURES` | No | — (none enabled) | Comma-separated list of feature flag ids to turn on experimental/in-development features, e.g. `MY_FLAG,OTHER_FLAG`. Use `*` to enable all of them. Leaving this unset enables none of them. Changing it requires a restart — there is no toggle in the Settings UI. An unrecognized flag id (e.g. left over from another branch) is logged as a warning and ignored — it never enables anything and never prevents the platform from starting. |

The resolved list of enabled flags is also sent to the frontend (as part of the public, boot-time `GET /auth/config` response), so a flag can control both backend behavior and whether a UI element is shown — with the backend independently enforcing the same flag on any route it gates.

**Evaluation** (datasets, evaluations and their runs, and the persona rewrites they measure) is **off by default**, and never available in XTM One mode. In Copilot or dev mode, turn it on with `ENABLED_FEATURES=EVALUATION` on the platform and the worker, then restart both; `*` turns it on too. Off, its pages and API are gone, no run starts (scheduled, on agent change, or from an agent's tools) and nothing it stored is deleted: turning it back on picks everything up again.

<a id="custom-dashboards"></a>**Custom dashboards** (`DASHBOARD_STUDIO`) are **off by default**. Turn them on with `ENABLED_FEATURES=DASHBOARD_STUDIO` on the platform and the worker, then restart both; `*` turns them on too. Off, the dashboards pages and API are gone (404), the `Custom Dashboards` built-in tool is neither offered nor run, published links answer like unknown ones and a home dashboard falls back to the platform's. Nothing is deleted: turning it back on restores everything.

**The mobile companion** (`MOBILE_COMPANION`) is **off by default**. Turn it on with `ENABLED_FEATURES=MOBILE_COMPANION` on the platform and the worker, then restart both; `*` turns it on too. Off, a phone gets the web app, linking a phone and its routes (`/api/v1/mobile-devices`, `/api/v1/mobile`, `/api/v1/app-lock`) answer 404, nothing is pushed to a phone, the companion PIN never locks anyone out, and agents are not handed the `phone` tool. Linked phones and PINs are kept: turning it back on picks them up again. See [Mobile companion](../usage/mobile-companion.md).

**Agentic coding labs** (`AGENTIC_CODING`) are **off by default**, and never available in XTM One mode: they are Filigran's own development tooling, not a product feature. In Copilot or dev mode, turn them on with `ENABLED_FEATURES=AGENTIC_CODING` on the platform and the worker and set `AGENTIC_LABS_NAMESPACE` (see [Agentic coding labs](#agentic-coding-labs) below); `*` turns the flag on too, and the labs stay off while the namespace is empty. Off, the sandbox service tools behave as before.

---

## Agentic coding labs

With `AGENTIC_CODING` on (Copilot or dev mode only), `sandbox_start_service` starts what a checkout declares as a service in a Kubernetes pod of its own, one pod per lab, in the namespace `AGENTIC_LABS_NAMESPACE`: a workspace container holding the checkout and one container per service, on the pod's shared `localhost`. The platform talks to the Kubernetes API server with its service-account token; that account needs `create`, `get`, `list`, `patch` and `delete` on `pods`, `get` on `pods/log` and `create` on `pods/exec` in that namespace, nothing else. A lab is never refused for a quota, never counted against one and never limited in number: the pod carries resource requests so the scheduler can place it, and no limits. Its preview (`http://<pod IP>:<port>/`) opens in the browser tool and `web_fetch` while the lab runs. The idle and deadline settings below are hygiene, not limits: `0` turns either off.

| Variable | Required | Default | Description |
|---|---|---|---|
| `AGENTIC_LABS_NAMESPACE` | No | — (labs off) | The Kubernetes namespace lab pods are created in. Empty keeps the labs off whatever `ENABLED_FEATURES` says. |
| `AGENTIC_LABS_API_URL` | No | In-cluster address | The Kubernetes API server. Empty uses `KUBERNETES_SERVICE_HOST` / `KUBERNETES_SERVICE_PORT`. |
| `AGENTIC_LABS_TOKEN_PATH` | No | `/var/run/secrets/kubernetes.io/serviceaccount/token` | The service-account token, read on every call so a rotated token is used. |
| `AGENTIC_LABS_CA_PATH` | No | `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` | The CA the API server's certificate is checked against. |
| `AGENTIC_LABS_CPU_REQUEST` | No | `250m` | CPU requested per container of a lab pod (no limit is set). |
| `AGENTIC_LABS_MEMORY_REQUEST` | No | `512Mi` | Memory requested per container of a lab pod (no limit is set). |
| `AGENTIC_LABS_START_SECONDS` | No | `600` | Seconds a lab pod may take to be scheduled and start its workspace container. |
| `AGENTIC_LABS_COPY_SECONDS` | No | `300` | Seconds one attempt of the checkout copy into a lab may take, apart from the start budget. A copy that times out or loses its stream is tried once more on the same pod before the start answers an error. |
| `AGENTIC_LABS_IDLE_HOURS` | No | `12` | A lab unused this long is deleted by the hourly sweep. `0` = never. |
| `AGENTIC_LABS_DEADLINE_HOURS` | No | `72` | Kubernetes ends a lab pod this old (`activeDeadlineSeconds`). `0` = never. |
| `AGENTIC_LABS_LICENSING_URL` | No | — (no licences) | Filigran's licensing server. At every lab start the platform asks its `/api/ci` route for one short-lived `ci` Enterprise Edition licence per entry of `AGENTIC_LABS_LICENCES`. |
| `AGENTIC_LABS_LICENSING_TOKEN` | No | — (no licences) | The licensing server's API-user token (permission to issue CI licences). Kept on the platform: it never reaches a lab pod, an agent or a log. |
| `AGENTIC_LABS_LICENCES` | No | — (no licences) | `ENV_VAR:product name,...`: each lab container gets the licence of that product in that variable. A licence that cannot be issued never stops the lab; the start answer names it. |
| `AGENTIC_LABS_LICENCE_HOURS` | No | `24` | Hours each lab licence is valid for (at most 168). |
| `AGENTIC_LABS_MODEL_KEY_ACCOUNT` | No | — (stub model) | Email of the account whose short-lived personal API key, scoped to the LLM proxy only, is minted for every lab that declares `LAB_MODEL = "live"`. The key is handed to the pod as `LAB_MODEL_API_KEY` and revoked when the lab stops; no provider credential ever reaches a lab. |
| `AGENTIC_LABS_MODEL_BASE_URL` | No | — (stub model) | The LLM proxy as a lab reaches it (`https://<host>/v1`), handed as `LAB_MODEL_BASE_URL`. The host must be in the labs' egress allow-list. |
| `AGENTIC_LABS_MODEL_NAMES` | No | — | Comma-separated model names a live lab's backend offers, handed as `LAB_MODEL_NAMES`. |
| `AGENTIC_LABS_MODEL_KEY_HOURS` | No | `12` | Hours a lab's model key is valid for (at most 72); revoked earlier when the lab stops or is reaped. |

---

## Presentation export

Exporting a [presentation](../usage/presentations.md) to PDF or PowerPoint is a **server-side render**: the platform opens the deck in its own headless Chromium — the same browser that already ships in the image for the `browser` tool and the PDF path of `generate_file` — at a fixed 1920x1080 slide size, and hands back the file. This is what makes an exported slide pixel-identical to the slide on screen, independently of the user's browser, zoom level or print dialog. Nothing needs to be installed for it; the unified and worker images both carry the browser.

The browser loads the deck's render page from the platform itself, authenticated with a short-lived token bound to that one deck (three minutes, read access to the deck's own images only, refused everywhere else in the API). Every other request the page could make — external fonts, scripts, images — is blocked; the deck is entirely self-contained.

| Variable | Required | Default | Description |
|---|---|---|---|
| `PRESENTATION_RENDER_BASE_URL` | No | Automatic | Origin the headless browser loads the render page from. Empty means automatic: the unified image uses its own loopback address (`http://127.0.0.1:4000`), so no request goes through the ingress and the pod does not need to resolve its own public hostname; a split development setup (Vite dev server + API) falls back to `FRONTEND_URL`. |
| `PRESENTATION_LAYOUT_CHECK_ENABLED` | No | `true` | Render a deck in the same headless browser right after an agent saves it and report what the schema cannot see — a slide body shrunk to fit, text cut by its box, text painted past the slide's edge — back to the agent in the tool result, so it fixes the slide before answering. Set to `false` to skip the check everywhere (tool result, `get_presentation`, `POST .../layout-check`; the viewer then offers no `Layout check` button), for example on a deployment without the headless browser. |

!!! tip "When to set it"
    Set `PRESENTATION_RENDER_BASE_URL` when the platform runs behind an ingress the pod cannot reach from the inside, or behind an authentication proxy that would intercept the render page before it reaches the platform — the render token is that page's only credential, and a proxy login screen in its place shows up as an export that fails with a rendering error. Point it at an address that reaches the platform container directly, for example the Kubernetes service name.

At most two exports render at the same time per process; further requests wait for a slot, and a user may request at most six exports a minute from the export buttons (decks and reports together; past that the request is answered `429` with a `Retry-After`). A render that has not settled after 60 seconds (fonts, images and diagrams all loaded) fails with an explicit error rather than hanging, and the `Print to PDF (browser)` action on a deck's chat card remains available as a fallback that needs no server rendering.

An agent-generated [report](../usage/reports.md) exports to PDF the same way, through the same browser, slots, timeout and `PRESENTATION_RENDER_BASE_URL`: the platform opens the report's render page (with a three-minute token bound to that one report) and prints it on A4, exactly as the report's viewer prints it, every annex open. The `Export` menu of a report (its chat card and its viewer) offers that PDF and `Print to PDF (browser)` as the fallback; an agent with the report tool exports it as a file of the conversation when the user asks for one.

### Layout check

A slide is a fixed surface, and a schema cannot tell whether six cards and a callout fit on it. So the same headless browser also serves as the **layout check** that runs after every save an agent makes: the deck is rendered, each slide is measured, and what the audience would see go wrong is reported to the agent in the tool result — a body that had to be shrunk to fit (with the ratio, so "dense" and "overloaded" are told apart), text cut off by a box or the footer, a badge whose text runs past its pill, anything painted past the slide's edge, a statement quote that runs past four lines, a chart with no room to write the values it was asked for (`values hidden on chart 2`, with the room it had and what to change), a chart whose values had to be turned on their side (`values rotated on chart 2`, advisory, with a smaller text size or a wider column as the remedy), a pie or a donut whose values had to be set smaller than its text size or written inside its slices (`values shrunk on chart 2`, advisory, with the legend under the chart or a wider column as the remedy), a row of key figures that had to shrink below its size and no longer matches the same row on the other slides — and, in the other direction, a content slide whose blocks leave more than half of its body bare and that no footer block closes (reported as advisory, with the remedies that slide actually has: more content, a footer block takeaway, or a merge with the neighbouring slide; blocks are sized to their content, so a card or a grid is never stretched to the floor), a content slide whose columns end more than a fifth of the body apart (`uneven`, advisory, with a block for the shorter column or shared card heights as the remedy), and a body of one block packed under the title over more than 30% of bare body (`floating`, advisory, with a centred placement or the content that carries it as the remedy). The agent then trims or fills the slide it is told about and saves again, instead of leaving it for the presenter to discover. On screen, a slide whose content does not fit is scaled down to fit rather than overlapping its footer, and the exported PowerPoint carries the same scaled sizes; a row of key figures keeps its size while the rest of the slide shrinks, so it reads the same on every slide. The check is bounded (45 seconds, then it is skipped with a note) and never fails a save; the same measurement is available on demand at `POST /api/v1/presentations/{id}/layout-check` to whoever can edit the deck (a read-only reader gets a `403`).

---

## Browser profiles

[Browser profiles](../usage/resources/browser-profiles.md) sign the platform's headless Chromium (the browser already in the image) in to a web application and keep the session, for the agents that browse with them and for Video Studio recordings. They need no feature flag. A sign-in with an interactive MFA runs where it was asked from and waits for the code through Redis, so any replica can receive it; the worker keeps the saved sessions signed in (each profile's "Keep the session alive").

| Variable | Required | Default | Description |
|---|---|---|---|
| `BROWSER_PRIVATE_TARGETS` | No | `false` | Let a browser profile open an application on a private network, on the hosts it declares (operator-network policy: loopback, link-local and cloud metadata addresses stay refused): its sign-in, the agents browsing with it and Video Studio recordings. Also in Settings > Access and security, where the variable, when set, wins and locks the field. It replaces `VIDEO_STUDIO_PRIVATE_TARGETS`, which is no longer read: a value an administrator saved in Settings > Access and security moves over by itself, a variable must be renamed. |

---

## Video Studio

The [Video Studio](../usage/video-studio.md) records demo environments in the platform's own headless Chromium (the browser already in the image), composes the video frame by frame in that same browser and encodes it with **ffmpeg**, which the unified and worker images ship. A recording that acts on a graph's node reads the label painted on the graph with **Tesseract** (`tesseract-ocr`), shipped in the same images; a worker without it finds such nodes only by the highlight the graph gives them. Recordings and renders run in the worker, as durable jobs; a sign-in with an interactive MFA runs where it was asked from and waits for the code through Redis, so any replica can receive it.

The Video Studio is **off by default**: a render needs a worker sized for Chromium and ffmpeg. Turn it on with `ENABLED_FEATURES=VIDEO_STUDIO` on the platform and the workers, then restart them. Size the workers first ([Sizing the workers](#sizing-the-workers) below):

- **CPU**: a request and a limit of 8, the same value. The render estimates from the CPUs the container's quota grants, so a request lower than the limit gives renders fewer CPUs than they were planned for.
- **Memory**: a request of 6 GiB and a limit of 24 GiB for the worker that draws whole renders.
- **Pages**: leave `VIDEO_STUDIO_RENDER_PAGES` at `0`.
- **Three workers**: one that draws whole renders and scene previews, and two that draw scene previews only (`VIDEO_STUDIO_RENDER_CONCURRENCY=0`). `docker-compose.yml` runs them as `worker` and two replicas of `worker-preview` (`docker compose up -d --scale worker-preview=N` for another count).
- **Restarts**: give the workers the same `WORKER_TERMINATION_GRACE_SECONDS` and `WORKER_STARTUP_SECONDS` as their pods' `terminationGracePeriodSeconds` and startup probe. A render under way when its worker is replaced waits that long for the next worker, then resumes where it stopped; 10 minutes when neither is set. With a `terminationGracePeriodSeconds` of `1260` (`docker-compose.yml` sets the same `stop_grace_period`), a worker asked to stop lets the renders it draws past half their frames finish, for up to 20 minutes (`VIDEO_STUDIO_RENDER_DRAIN_SECONDS`), and hands the others to a running worker at once. A render handed over keeps its slot: the worker that takes it back gives it priority over the renders queued after it.

On that worker, the estimate for a 130 s, 30-scene 4K video is about 45 minutes, before any render of the platform has been measured. A render that one worker cannot draw within 4 hours is drawn in scene ranges, each on a worker of its own, so more worker replicas also make long 4K renders faster. While it is off, `Work > Videos`, the Video Studio tool, the `Video Production` skill, the built-in video templates and the `Video Generation` and `Music Generation` models in **Settings → AI Models** are not offered, and public video links stop answering. Videos already made are kept and come back when it is turned on again.

### Sizing the workers

A worker's memory is shared between its whole renders, its scene previews and its own jobs:

- **The preview reserve.** A worker that draws scene previews keeps 8 GiB that no render ever takes. This is a hard floor: `VIDEO_STUDIO_PREVIEW_RESERVE_MB` may raise it, never lower it. A worker with `VIDEO_STUDIO_PREVIEW_CONCURRENCY=0` draws no preview and keeps no reserve.
- **A render's ceiling.** A whole render may use 60% of the container's memory limit (`VIDEO_STUDIO_RENDER_MEMORY_MB` when set). A 4K (`ultra`) render may use `VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB` (16 GiB). Neither ever goes past the limit less the reserve. A worker no bigger than its reserve draws no render at all, and its renders are refused with the memory to give it.
- **A render's pages.** A render draws on one page per CPU but one, as many as its ceiling holds at each page's measured peak plus 20%. At 2160p a page peaks at about 3.7 GiB, and 4.6 GiB while it composes a screen insert. The pages are laid out when the render starts, in what the worker has then. From then on, the render holds the budget of the pages it opened, and the rest of its ceiling is left to the previews.
- **A scene preview.** A preview draws on its 2 pages, never on one for want of memory. Its ceiling is twice its estimate, kept within the reserve: about 2.7 GiB for a 720p draft, 4.5 GiB at 1080p, 5.4 GiB at 1080p with a screen insert, and 7.5 GiB with three video inserts and a picture. The reserve therefore always holds one preview beside a 4K render. When the worker that took a preview cannot hold its pages, the preview goes to the worker with the most memory free and a preview slot free. When no worker has room, it waits for its pages where it is. Its answer says which, and what holds the worker's memory: its renders and its previews, each with their memory. A worker with the default reserve and 2 preview slots runs its second preview beside a 4K render only once the first ends. When a preview waited for that, its answer says once, for the administrator, the `VIDEO_STUDIO_PREVIEW_RESERVE_MB` that holds every slot at its worst case (16384 for 2 slots) and the memory to add to the container. A second worker that draws scene previews only does the same.

With 8 CPUs and the video of the measured fixture (screen inserts on screen):

| Container memory limit | Whole render (`high`) | 4K render (`ultra`) | 4K pages, without / with screen inserts | Left to previews during that 4K render |
|---|---|---|---|---|
| 16 GiB | 8 GiB | 8 GiB | 1 / 1 | 10 GiB |
| 24 GiB (recommended) | 14.4 GiB | 16 GiB | 4 / 3 | 8.6 GiB |
| 32 GiB, `VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB=24576` | 19.2 GiB | 24 GiB | 6 / 4 | 11.6 GiB |
| 48 GiB, `VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB=40960` | 28.8 GiB | 40 GiB | 7 / 7 | 13.3 GiB |

A 4K render with screen inserts takes about 35 GiB to draw on all 7 pages: give the worker a limit of 48 GiB and raise `VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB` to match. When a render opens fewer pages than the worker has CPUs, the first pre-flight to see it says so once, with the time on the pages opened against the time on all of them and the memory to give. It is said again only when those numbers change.

**Workers.** The preview workers draw scene previews only, so a 4K render on the first worker never leaves the previews short of memory. They run every other job of the platform too. The preview queue takes one preview per video in turn, and a sweep of every scene of a video queues a preview per scene: with two preview workers, another video's preview starts when the next preview slot frees, not after the whole sweep. A queued preview's answer names the videos whose previews it waits behind, and how many, and how many workers draw previews ("1 worker draws previews, 2 at a time"), counted from the workers that are running, not from what the deployment declares. A preview that waited behind other videos' previews with a single preview worker says once, under platform sizing, that a second one is missing. On Kubernetes, run the same set: one worker deployment that renders, and one with `VIDEO_STUDIO_RENDER_CONCURRENCY=0` and 2 replicas. A worker that renders only (`VIDEO_STUDIO_PREVIEW_CONCURRENCY=0`) keeps no reserve. Give each worker a memory limit of its own: a worker without one reads the host's memory, and two workers on one host would each count all of it. A long render is split into scene ranges across the workers that draw renders, never onto a preview-only worker. While several videos have previews in the queue, each holds at most its share of the preview slots (the slots over the videos, rounded up), so one video's sweep never keeps every slot while another video's preview waits for its memory or for another worker. A queued preview's answer also says how long it should wait at the queue's pace: its place, the slots, and the median time of the last 20 previews drawn ("about 20 min before it starts at the lane's pace").

**A render's CPUs against the other jobs.** A whole render's pages share their worker with whatever else it runs: scene previews and recordings, but also the platform's assignment runs, sub-agent tasks and flows, which run in the worker's own process. Scene previews always draw at background priority. A whole render draws in a render lane of its own where the worker allows it: where the worker's container has its own cgroup v2 writable (`/sys/fs/cgroup`, a delegated cgroup), the worker moves its processes to a `xtm-worker` child cgroup and each whole render to a `xtm-render` one weighted `VIDEO_STUDIO_RENDER_CPU_WEIGHT` (1000 against the worker's 100), so that on a busy CPU the render keeps about 90% of it however many jobs run beside it; a process started later in the container (`docker exec`, `kubectl exec`) joins the worker's. Where the cgroup is read-only, which is the default for Docker and Kubernetes, a worker granted `CAP_SYS_NICE` (Compose: `cap_add: [SYS_NICE]`; Kubernetes: `securityContext.capabilities.add: ["SYS_NICE"]`) starts its renders at a raised priority with the same ratio instead. Without either, a render draws at the worker's own priority, and its estimate says so with what to grant. Whatever the lane, while a worker draws a whole render, new assignment runs, sub-agent tasks and evaluations go to a running worker that draws none (with the default Compose file, the preview workers) and run beside the render only when no such worker takes them within a few seconds. The render's estimate prices the jobs it shares its worker with at the CPU that worker measured for them over the last minute, and names them.

**A GPU for the recordings.** A recording draws the graphics a page draws itself, in 2D or with WebGL (a graph laid out again, a 3D view orbiting), on the worker's GPU when its Chromium finds one, and on the CPU (SwiftShader) otherwise. On a worker without a GPU, a graph's layout moves may be captured at 5 to 16 frames a second and play stepped. There, each graph is also captured on its own, at reduced quality, and where the capture of the whole page falls short of a layout move, the move is put together from those captures over the page's last full frame, so it keeps 20 frames a second or more while it moves (sharp again once it stops). A move that still plays mostly held frames is said at its step, naming the graph's element and "software GL", and the check when you save a video showing it says "stepping motion". Which worker shape uses what: a GPU worker draws on its GPU (GPU GL) and captures a graph with its page; a CPU worker draws on SwiftShader (software GL) and captures a graph on its own as above. To record on a GPU, run a worker on a GPU node with the GPU passed through and its graphics driver exposed (NVIDIA: the container toolkit with `NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics`, so Chromium finds the Vulkan driver), and keep the recordings on it: `VIDEO_STUDIO_TAKE_CONCURRENCY=0` on the other workers. Each recording says what it was drawn with (GPU GL, or software GL and why), and the worker logs it once at its first recording.

### Video Studio settings

| Variable | Required | Default | Description |
|---|---|---|---|
| `VIDEO_STUDIO_FFMPEG_PATH` | No | `ffmpeg` on `PATH` | The ffmpeg binary that encodes recordings and renders. |
| `VIDEO_STUDIO_FFPROBE_PATH` | No | `ffprobe` on `PATH` | The ffprobe binary that reads media durations and sizes. |
| `VIDEO_STUDIO_MAX_TAKE_SECONDS` | No | `300` | The longest recording, in seconds. |
| `VIDEO_STUDIO_MAX_VIDEO_SECONDS` | No | `1200` | The longest video a render produces, in seconds. |
| `VIDEO_STUDIO_RENDER_CONCURRENCY` | No | `1` | Whole renders one worker runs at the same time (each holds Chromium and ffmpeg). Scene previews have a lane of their own (`VIDEO_STUDIO_PREVIEW_CONCURRENCY`). While a worker has no whole render to make and its preview slots are all busy, these slots take the next scene previews too; a whole render asked meanwhile waits for that one preview to end. `0` = this worker draws no whole render and no scene range: scene previews only (`worker-preview` in `docker-compose.yml`). |
| `VIDEO_STUDIO_PREVIEW_CONCURRENCY` | No | `2` | Scene previews (one scene, at 720p30 or, for a scene showing a figure, a recording or a screen insert, 1080p30) one worker runs at the same time, beside its whole renders, on slots of their own; the queue takes the videos in turn. A preview starts on its 2 pages only, in what the worker's renders and previews leave of its memory limit, which is never less than the preview reserve: on a 24 GiB worker running a 16 GiB `ultra` render, a preview of up to 8 GiB starts at once. A preview whose pages the worker does not hold goes to the worker with the most memory free and a free preview slot, or waits for its pages, with no attempt spent. Its answer says which. `0` = this worker draws no preview and keeps no reserve. See [Sizing the workers](#sizing-the-workers). |
| `VIDEO_STUDIO_PREVIEW_CONCURRENCY_MAX` | No | `0` | The most scene previews one worker runs at the same time while previews are queued. Above `VIDEO_STUDIO_PREVIEW_CONCURRENCY`, the worker adds a preview slot for each preview queued beyond what its slots run, as many as the CPUs its whole renders leave hold (2 CPUs a preview, on its 2 pages), and drops them as the queue empties: a preview already running keeps its slot. A queued preview's answer says the slots added (`preview_slots_added`). Raise it only on a worker with CPUs to spare beyond its own slots: on a worker of 4 CPUs and 2 slots it adds none. `0` = the slots stay `VIDEO_STUDIO_PREVIEW_CONCURRENCY`. |
| `VIDEO_STUDIO_TAKE_CONCURRENCY` | No | `2` | Recordings one worker runs at the same time. `0` = this worker records nothing, so the recordings run on the workers that do: keep them on a GPU worker this way (see [Sizing the workers](#sizing-the-workers)). At least one worker must record. |
| `VIDEO_STUDIO_RECORDING_GL` | No | `auto` | What a recording's browser draws a page's WebGL and 2D graphics on. `auto` = the worker's GPU when Chromium finds one to draw WebGL on (asked once, at the worker's first recording), the CPU (SwiftShader) otherwise. `software` = always the CPU, never looking for a GPU. `gpu` = the recording lane of a GPU node: the worker probes its GPU at start (ANGLE on Vulkan, then on EGL) and logs the WebGL renderer, and records only once Chromium draws WebGL there; a worker that reads a CPU rasterizer (SwiftShader, or Mesa's llvmpipe, softpipe or lavapipe, which Chromium reports as enabled WebGL) claims no recording at all and names what each launch read and what its container holds of the NVIDIA driver (the `/dev/nvidia*` nodes, `NVIDIA_DRIVER_CAPABILITIES` and `NVIDIA_VISIBLE_DEVICES`, whether `libEGL_nvidia.so.0` and `libGLX_nvidia.so.0` load or the loader's error naming the missing file, the EGL vendor and Vulkan ICD files, and what libEGL answers under the lane's own EGL environment: its device enumeration and platform extensions, the EGL devices it lists and `eglInitialize` on the device, surfaceless and default displays), plus the GPUs Chromium's GPU process saw on each launch. A queued recording shows that reason whole. So give it the NVIDIA runtime with the `graphics` driver capability and keep `VIDEO_STUDIO_TAKE_CONCURRENCY=0` on the other workers. On the CPU, a 60 fps recording of a page drawing graphs steps on their transitions: the recording says so and offers 30 fps, and a video's check says each transition on every scene showing it - a brief one the audience still reads as motion as advice, the others as a finding the render waits for. |
| `VIDEO_STUDIO_CLIP_CONCURRENCY` | No | `4` | AI clip generations one worker waits on at the same time. |
| `VIDEO_STUDIO_MAX_RENDERS_PER_USER` | No | `10` | Renders one person may have queued or running at once, across all their videos. Beyond it a render is refused with the ones in flight listed; the others wait in the queue with their position. Scene previews are counted apart, per video: every preview asked is queued, and at most this many previews of one video run at once. |
| `VIDEO_STUDIO_CLIP_INLINE_WAIT_SECONDS` | No | `200` | How long the assistant's `generate_video_clip` waits for its shot inside the call, the same for every call of a batch, counted from the call's start and never more than the call's own deadline allows. Past it the shot finishes in the background and the answer says why (waiting for a free generation slot, queued at the provider, rate limited). `0` = always in the background. |
| `VIDEO_STUDIO_RENDER_PAGES` | No | `0` | Headless pages one render draws its frames with, each encoding its own run of the video. `0`, the default, follows the rule: the CPUs of the worker container's quota minus one, the CPU left being the worker's own (8 CPUs draw on 7 pages). A render gets faster with more pages as long as each page has a CPU to use. A render never draws on more pages than the worker has CPUs, nor on more than its memory ceiling holds, each page at its measured peak plus a 20% margin: at 2160p one page peaks at about 3.7 GiB, and 4.6 GiB while it composes screen inserts, so an `ultra` render draws on 4 pages within its 16 GiB, or 3 for a video with screen inserts; drawing on 7 pages with inserts takes a ceiling of about 35 GiB. The pages are laid out when the render starts, in the memory the worker has then. Each page's encoder runs on its share of the CPUs. A scene preview draws on 2 pages at most. Give the container a CPU request as high as its limit. A number sets the pages instead, never more than the CPUs. Each render records the pages and CPUs it used and its frames per second, and the next renders' estimates start from them. |
| `VIDEO_STUDIO_RENDER_CPU_WEIGHT` | No | `1000` | The CPU weight a whole render's pages and encoders draw at against the 100 of every other job of the worker (assignment runs, scene previews, recordings): a cgroup v2 `cpu.weight` where the worker's container cgroup is writable, else a raised priority of the same ratio where the worker holds `CAP_SYS_NICE` (see [Sizing the workers](#sizing-the-workers)). `1` to `10000`. `0` = a render draws at the worker's own priority. |
| `VIDEO_STUDIO_RENDER_MAX_RANGES` | No | `8` | The most scene ranges a render is split into when one worker cannot draw it within 4 hours. Each range is drawn and encoded on a worker of its own, within 4 hours of its own, and the render joins them and mixes the sound once. A range no other worker takes is drawn by the render's own worker after its range, so a single worker still renders it, one range after another. `1` = a render is never split: an `ultra` render then runs past 4 hours in segments, up to 8. |
| `VIDEO_STUDIO_RENDER_SPLIT_MINUTES` | No | `30` | A render that would take longer than this many minutes on the pages its worker's memory opens is split into scene ranges across the workers running, as many ranges as bring each one under it (one a worker at most, `VIDEO_STUDIO_RENDER_MAX_RANGES` at most), so a long `ultra` render finishes sooner on a platform with several workers. With a single worker it is drawn whole. `0` = a render is split only past the 4 hours it may run. |
| `VIDEO_STUDIO_FRAME_CAPTURE` | No | `screenshot` | How a render takes each frame from its headless page. `screenshot` captures what the page's compositor shows, all pages in one browser. `begin_frame` asks Chromium to produce the frame on demand (`HeadlessExperimental.beginFrame`), 13-29% faster in the render image depending on the scenes, each page drawing in a browser of its own; it is opt-in. `auto` is `begin_frame` on Linux and `screenshot` elsewhere. A page whose frames cannot be begun takes screenshots by itself. Both draw the same frames. Each worker checks `begin_frame` once when it starts: its Chromium must produce one frame within 2 seconds, else that worker takes screenshots. A worker whose begun frames fail a render takes screenshots until it restarts. The render estimate says which capture the worker uses and why. |
| `VIDEO_STUDIO_RENDER_MEMORY_MB` | No | `0` | Each render runs in a process of its own; once that process and everything it started (Chromium, ffmpeg) use more than this many MiB, the render is stopped and failed with its peak memory and the scene it was drawing, and nothing else in the worker is touched. `0` = 60% of the worker container's memory limit (4096 MiB when the container sets none). A render whose pages this does not hold, even one page, is not started. The refusal says the memory to give the worker, then the highest quality that fits the worker now, for the person to choose. An `ultra` render is held to `VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB` instead when that is higher. Never more than the container's limit less the preview reserve. |
| `VIDEO_STUDIO_ULTRA_RENDER_MEMORY_MB` | No | `16384` | The memory ceiling of an `ultra` (2160p60) render, in MiB, at least. Where the worker container has more memory, it is raised to what the pages its CPUs open need at their measured 2160p peak (about 5.9 GiB a page), so a 4K render draws on more pages instead of 2. It is never more than the worker container's memory limit (the host's memory when the container sets none) less 4 GiB kept for the worker's own jobs or the preview reserve, whichever is larger, and never less than `VIDEO_STUDIO_RENDER_MEMORY_MB`'s ceiling. Raising it past the limit less the reserve gives nothing: give the container more memory too. See [Sizing the workers](#sizing-the-workers). `0` = an `ultra` render is held to the same ceiling as the others. |
| `VIDEO_STUDIO_PREVIEW_RESERVE_MB` | No | `8192` | The memory, in MiB, every worker that draws scene previews keeps out of every render's reach. It is a hard floor of 8192: a lower value keeps 8192, a higher one raises it. A render's ceiling is never more than the container's memory limit (the host's memory when the container sets none) less this, and its pages are laid out in what is left when it starts, so a scene preview always starts beside it on its pages. A worker with `VIDEO_STUDIO_PREVIEW_CONCURRENCY=0` keeps none. |
| `VIDEO_STUDIO_PREVIEW_MEMORY_MB` | No | `0` | The memory ceiling of a scene preview's process, in MiB: past it the preview is stopped, and nothing else in the worker is touched; it is drawn again at once on one page, and only failed, with its peak memory, when one page does not fit either. `0` = twice the estimate of the preview at its own size on its 2 pages, the screen inserts the scene shows counted on each page (about 2.7 GiB for a 720p preview without inserts and 4.6 GiB with three video inserts and a picture; about 4.5 GiB and 7.5 GiB at 1080p), kept within the preview reserve and never more than the worker's memory. |
| `VIDEO_STUDIO_INSERT_FRAMES_MB` | No | `1024` | The screen-insert frames, in MiB, a worker keeps in its temporary directory (`/tmp`, beside the renders' working directories). A frame of a screen insert costs many times a plain frame to draw; once drawn, every render and scene preview on that worker that shows it again at the same size reads it back instead - the next preview of a scene with an insert takes about the time of a scene without one. A 10 s insert at 1080p keeps about 150 to 300 MiB. Never more than a quarter of what that disk has free; the least recently used go first, and what nothing read for 6 hours. Size the worker's `/tmp` for it: a Kubernetes `emptyDir` size limit (the Helm chart's `tmpSizeLimit`) must hold it with the renders' working directories, or the pod is evicted. `0` = no frame is kept. |
| `VIDEO_STUDIO_RENDER_STALL_SECONDS` | No | `120` | A render whose progress stands still this many seconds, at any stage, is stopped and resumed in place with the frames it had finished kept; after two resumes it is failed as "stalled at X%". `0` = never. |
| `VIDEO_STUDIO_RENDER_DRAIN_SECONDS` | No | `1200` | How long a worker asked to stop (a deployment, a restart) waits for the whole renders it draws past half their frames to finish before it hands them to the next worker, counted from the stop signal. It never waits past `WORKER_TERMINATION_GRACE_SECONDS` less 30 seconds for the hand-over, so it waits only where the deployment gives its workers a longer termination grace than Kubernetes' default 30 seconds. A render under half its frames goes to a running worker at once, and a scene preview is never waited for. An idle worker stops as before. `0` = never waits. |
| `WORKER_TERMINATION_GRACE_SECONDS` | No | `0` | The worker pod's `terminationGracePeriodSeconds` (Docker Compose: its `stop_grace_period`), as its deployment sets it. With `WORKER_STARTUP_SECONDS`, how long a render the worker hands back when it is stopped waits for the next worker: first in the queue, it is failed only once neither its old worker nor a new one could have taken it. It also bounds how long a stopping worker waits for its renders to finish (`VIDEO_STUDIO_RENDER_DRAIN_SECONDS`). `0` = not given: 30 s, the Kubernetes default. Never less than 3 minutes together. `docker-compose.yml` sets `1260` for its workers. |
| `WORKER_STARTUP_SECONDS` | No | `0` | How long a new worker pod may take to start: its startup probe's `initialDelaySeconds` plus `failureThreshold` x `periodSeconds`, with the image pull if it is slow. `0` = not given: 570 s, so a deployment that sets neither lets a render wait 10 minutes. A render that waited longer fails naming the workers alive and why none took it; rendering it again at the same quality within a day resumes the same render from the frames it kept. |
| `VIDEO_STUDIO_MEDIA_RETENTION_DAYS` | No | `30` | Days a recording's or an AI shot's media files are kept once no video uses them any more (removed from the last timeline using them, or that video deleted). The media a video uses are never deleted. A rendered video keeps its own MP4; the recording or shot stays listed, marked expired, to be recorded or generated again. `0` = kept forever. |
| `VIDEO_STUDIO_UNUSED_MEDIA_RETENTION_DAYS` | No | `14` | Days the media of a recording or an AI shot no video ever used are kept after they were made. `0` = kept forever. "Keep its media" on a recording or a shot exempts it from both. |
| `VIDEO_STUDIO_FIGURE_STILL_RETENTION_DAYS` | No | `30` | Days the editor's still of a figure scene is kept after it was drawn. A still a video still shows is drawn again by the worker the next time the video is saved or previewed. `0` = kept forever. |
| `AGENT_CHECK_FILE_RETENTION_DAYS` | No | `7` | Days the pictures an assistant makes to check its own work (frames and region crops of a video, contact sheets, screen-insert sweeps, slide captures, page renders) are kept after they were made. They are working files, collapsed under the reply; a picture a video uses (an image scene, a backdrop) is kept. `0` = kept forever. |
| `FILE_RETENTION_AGENT_WORKING_DAYS` | No | `60` | Days an assistant's working (scratch) file is kept after its **last use** (a download, a preview, a read by an assistant, an attach). Editable in **Settings > Usage and quotas > Storage & retention**; set here, it is locked there (the same for every `FILE_RETENTION_*` variable and `AGENT_CHECK_FILE_RETENTION_DAYS`). A file an open work item, a workset, an active shared state, a presentation, a report or a video still uses is kept. The content goes; the file stays in the conversation as a deleted card and its download answers 410 Gone. `0` = kept forever. |
| `FILE_RETENTION_AGENT_DOWNLOAD_DAYS` | No | `60` | The same, for the files an assistant delivers for download. |
| `FILE_RETENTION_AGENT_DAYS` | No | `0` | Days the other files assistants, tools and MCP servers return are kept after they were made. `0` = kept forever. |
| `FILE_RETENTION_CHAT_DAYS` | No | `0` | Days the files people attach in a conversation are kept after they were uploaded. `0` = kept forever. |
| `FILE_RETENTION_CHANNEL_DAYS` | No | `0` | Days the files received from Teams, Outlook and the other channels are kept. `0` = kept forever. |
| `VIDEO_QUALITY_CHECK_ENABLED` | No | `true` | Play every saved video in headless Chromium to check what the audience would see go wrong (text covering text, text past the frame, words on screen too briefly) and report it to the assistant and the editor. `false` skips the check; the layout that keeps text clear of the subtitles and overlays applies either way. |

Voice-over and AI clips use the AI providers ([Settings > AI Models](../administration/settings.md)) the person may use: OpenAI (speech, Sora) and Google Gemini (speech, Veo). Nothing else is configured for them.

How long renders are kept is not an environment variable: administrators set it in **Settings > AI Models > Render retention** (see [Render retention](../administration/settings.md#render-retention)). `VIDEO_STUDIO_RENDER_KEEP_LAST` and `VIDEO_STUDIO_RENDER_RETENTION_DAYS` are no longer read.

---

## Waiting arcade

While an agent works, the chat shows a small self-playing Space Invader. Users can take control of it and play a full-screen game over the interface, with a leaderboard shared by everyone on the platform — see [The waiting arcade](../usage/chat.md#the-waiting-arcade). Scores are stored in the platform's database; a game that cannot reach the server still plays, it just is not saved.

| Variable | Required | Default | Description |
|---|---|---|---|
| `ARCADE_ENABLED` | No | `true` | Offer the playable arcade and its leaderboard. Set to `false` to keep only the self-playing waiting animation: every `/api/v1/arcade` route then answers `404`, the chat offers no way to take control, and a `?arcade=play` link (the invitation of the chat embedded in OpenCTI, OpenAEV and OpenCRQ) just opens the page. This is the only switch: users cannot hide the game themselves, and it is left out only for a user whose system asks for reduced motion. |

---

## Skill loading

An agent's prompt carries a catalog of its attached skills (name + description) and the agent pulls a skill's full content in on demand with the `load_skill` tool, so prompt size scales with the *number* of attached skills rather than with the size of their content. A skill that must stay in the prompt at all times can be pinned per agent on the agent's `Configuration` tab.

These caps bound the worst case and rarely need changing:

| Variable | Required | Default | Description |
|---|---|---|---|
| `SKILL_LOAD_MAX_CHARS` | No | `60000` | Maximum characters returned by one `load_skill` call. Longer content is truncated with an explicit marker naming how much was cut. |
| `SKILL_LOAD_BUDGET` | No | `8` | Maximum number of distinct skills one run may load. Re-loading an already-loaded skill never counts again. `0` = unlimited. |
| `SKILL_RESOURCE_MAX_BYTES` | No | `40000` | Maximum bytes returned by one `read_skill_resource` call (files bundled with a packaged skill). |
