# Publishing XTM One's MCP servers in MCP catalogs

XTM One exposes six MCP endpoints. Five of them are meant for a person's MCP
client and are published in catalogs; this page says how, and what
each catalog asks of the server. The server-side work it relies on landed
with it: tool annotations, protocol negotiation and the current
Streamable HTTP rules, Client ID Metadata Documents.

## The constraint: one instance per customer

Every customer runs their own instance (self-hosted, or a Filigran-managed
private instance), so there is no single URL to publish. Both catalogs that
matter accept that:

- **MCP Registry**: a remote's URL may be a template whose variables the
  client asks for at install time (schema `2025-12-11`). Our entries use
  `https://{xtm_one_host}/mcp/<endpoint>`. The registry checks only that the
  filled URL is HTTPS and not loopback, and that every variable is declared.
- **Claude Connectors Directory**: the submission's "URL pattern" option
  takes an anchored regular expression every customer URL must match, with
  DCR or CIMD as the client registration (CIMD preferred).

## The registry entries

`mcp-registry/<name>.server.json`, one per endpoint:

| Entry | Endpoint |
| --- | --- |
| `io.github.XTM-One-Platform/xtm-one-platform` | `/mcp/xtm` |
| `io.github.XTM-One-Platform/xtm-one-agents` | `/mcp/user` |
| `io.github.XTM-One-Platform/xtm-one-opencti` | `/mcp/opencti` |
| `io.github.XTM-One-Platform/xtm-one-openaev` | `/mcp/openaev` |
| `io.github.XTM-One-Platform/xtm-one-opencrq` | `/mcp/opencrq` |

- **Not published**: `/mcp/global`. It authenticates with the instance-level
  `mcp-...` key an administrator holds, not with the person installing the
  client.
- **The variable** is namespaced (`xtm_one_host`, not `host`): VS Code merges
  equal input definitions across servers and refuses conflicting ones, so a
  generic `host` would collide with other vendors' entries, while the same
  definition in our five entries is asked once. Its value may carry the
  instance's `BASE_PATH` (`example.com/xtm-one`).
- **No header** is declared: OAuth (CIMD or DCR) needs none, and an optional
  `Authorization` header left empty would make a client send a broken one
  instead of signing in. API-key users keep the snippets of Profile > MCP
  Endpoint.
- **No `repository`**: the repository is not public, and the field exists so
  people can inspect the source.
- **`websiteUrl`** and the icon point at the public docs
  (`https://docs.xtmone.io/latest/`).

`backend/tests/test_mcp_registry_entries.py` holds the entries to the vendored
schema (`mcp-registry/schema/`, pinned by checksum), to the registry's semantic
URL checks, to the namespace, and to the routes the backend actually serves: a
new MCP endpoint fails it until it gets an entry or a reason in `UNPUBLISHED`.

## Publishing

`.github/workflows/mcp-registry.yml` runs on a release tag (`MAJOR.YYMMDD.SEQ`;
LTS tags are skipped, the entries do not change with them), or by hand ON a
release tag to finish a partly failed publish. The version is always the
tag's: there is no free-form input, since registry versions are immutable and
the newest one is what clients see.

1. it writes the version into every entry (the repository keeps `0.0.0`);
2. installs `mcp-publisher` at a pinned version, checked against a SHA-256
   pinned in the workflow (a replaced release asset fails the check);
3. runs `mcp-publisher validate` on every entry;
4. logs in with **GitHub OIDC** (`login github-oidc`): the registry grants the
   namespace `io.github.<repository owner>/*`, with no secret to manage;
5. publishes every entry the registry answers `404` for at that version, so
   re-running a partly failed publish finishes it; any answer other than
   `200` (already there) or `404` stops the job. It logs in again before each
   entry: the registry token lives 5 minutes.

**Who else can publish.** The registry grants `io.github.<owner>/*` to the OIDC
token of ANY workflow of ANY repository of the owner that may mint one, not
only this one. So this job runs only on a release tag, behind the protected
environment `mcp-registry` (required reviewers, deployments from tags only),
with `actions/checkout` pinned to a commit and no token kept; and no other
workflow of the organisation may request `id-token: write` without the same
care. `backend/tests/test_mcp_registry_entries.py` holds the parsed workflow
to these properties.

It does nothing until a repository administrator sets the repository variable
`MCP_REGISTRY_PUBLISH` to `true`: merging the workflow publishes nothing by
itself. To move to the `io.filigran/*` namespace instead, the domain needs a
DNS TXT record and the workflow `mcp-publisher login dns` with the key as a
secret; the entries' names change with it.

### Go-live

Registry versions are public and permanent, and the registry does not fetch
`websiteUrl` or the icons (it only checks they are HTTPS URLs):

1. An administrator creates the environment `mcp-registry` (required
   reviewers, deployment branches and tags: release tags only) and a tag
   ruleset limiting who may create `*.*.*` tags.
2. Merge, and wait for the `docs-pages` deploy, so `websiteUrl` and the icon
   resolve.
3. Run `mcp-publisher validate` on each entry from a branch (no login needed).
4. Set `MCP_REGISTRY_PUBLISH` to `true`, then run the workflow on the release
   tag and approve the deployment.
5. Remove the "Availability" note from `docs/docs/usage/resources/mcp-catalogs.md`
   once the entries are listed.

**Rollback**: `mcp-publisher status --status deprecated|deleted --message
"<why>" <name> [version]`, with the same login.

## The Claude Connectors Directory

Submitted from a Claude organization's admin portal (Team or Enterprise plan).
What the directory checks, and where XTM One stands:

| Requirement | XTM One |
| --- | --- |
| Streamable HTTP | Yes (SSE is no longer accepted by the directory). |
| `401` with `WWW-Authenticate: Bearer resource_metadata=...` | Yes, per endpoint. |
| Protected resource `resource` equal to the URL the user enters | Yes, `BASE_URL` + the endpoint path. |
| DCR or CIMD for a URL pattern listing | Both. Claude prefers CIMD once `client_id_metadata_document_supported` is advertised. |
| PKCE S256, `none` in `token_endpoint_auth_methods_supported` | Yes. |
| Redirect `https://claude.ai/api/mcp/auth_callback`, Claude Code's loopback on any port | Yes: any HTTPS redirect a client declares, an `http` loopback one declared without a port matched whatever its port. |
| Refresh tokens rotated, `invalid_grant` when one is no longer valid | Yes. |
| Every tool titled and annotated read-only / destructive, names of 64 characters at most | Yes. |

What Filigran provides with the submission, outside this repository:

- **The URL pattern**, for example
  `^https://[a-z0-9.-]+(/[a-z0-9._~-]+)*/mcp/xtm$` (lowercase host: Claude
  lowercases it before matching). One listing per endpoint.
- **A test instance** reachable from Anthropic's egress range
  (`160.79.104.0/21`), with the server enabled, a connected platform for the
  OpenCTI / OpenAEV / OpenCRQ listings, a reviewer account with realistic
  sample data, and a way to reset what the destructive tools change.
- **A public privacy policy** (a missing one is an immediate rejection) and
  public documentation: `docs/docs/usage/resources/mcp-catalogs.md`.
- **Example prompts** per listing.

## Operating notes for a customer instance

- A claude.ai custom connector connects from Anthropic's servers: the instance
  must be reachable from the internet. Claude Code connects from the user's
  machine.
- A hosted client that cannot use CIMD registers one client per fresh
  connection, from its provider's addresses: raise `MCP_DCR_RATE_LIMIT` (5 per
  hour per client IP by default) on an instance many people connect to that
  way.
- Behind a path prefix, the RFC 8414 / RFC 9728 path-inserted well-known
  documents must be forwarded from the host root (see the installation guide).
