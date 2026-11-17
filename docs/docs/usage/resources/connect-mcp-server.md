# Connect an external MCP server

An external MCP server gives your agents the tools of another system — GitHub, a SIEM, a sandbox, a threat-intelligence feed — through the [Model Context Protocol](https://modelcontextprotocol.io). This page explains how to configure one so XTM One can call it. For a tour of the screens once a server exists, see [MCP servers](mcp-servers.md). For the opposite direction — an external MCP client calling XTM One — see [Platform MCP servers](platform-mcp-servers.md).

## Before you start

- **Edition.** Adding or importing a server requires AI to be switched on and the platform to be licensed for agentic AI (Enterprise Edition, see [Editions and licensing](../../administration/editions-and-licensing.md)). When AI is switched off, the action shows the AI-disabled notice; when the agentic AI license is missing, it opens the Enterprise Edition dialog.
- **Who.** Any user can add a server for themselves. Only an administrator can create a `Company-managed server`. Every user of the platform can see and use it, unless an administrator turns on `Restrict access` in its sharing settings to limit it to named people and groups; only administrators can edit it.
- **What you need from the provider.** For a remote server, its endpoint URL and either an API key or an OAuth login. For a local (subprocess) server, the package or binary to run and the environment variables it expects.

## Choose a transport

| Transport | Use it when | What XTM One does |
|---|---|---|
| `Streamable HTTP` | The provider hosts the server and gives you a URL (usually ending in `/mcp`). The recommended choice for remote servers. | Connects to the URL over HTTP, sending your headers or OAuth token. |
| `SSE (legacy)` | The provider only offers the older Server-Sent Events endpoint (usually ending in `/sse`). | Same as above, over the legacy protocol. |
| `Subprocess (stdio)` | The server is distributed as a package or binary you run yourself (`npx …`, `uvx …`). | Starts the command as a child process of the XTM One backend and worker, and talks to it over its standard input and output. |

## Add a server

1. In the left navigation, open `Tools & Integrations > MCP Servers`.
2. Under `Connected MCP Servers`, click `Add Server`.
3. Pick a preset, or `Custom Server` to configure any server by hand. A preset fills the transport, command or URL and the expected variables, and shows a short note about where to get the credentials it needs (for example, the scopes of a GitHub token).
4. Fill in the form described below, then click `Create Server`.
5. On the server page, click `Test connection` (see [Test the connection](#test-the-connection)).
6. Attach the server to an agent (see [Give an agent access](#give-an-agent-access)).

Every server needs a `Server Name`. Names are labels for people; the tools the model sees keep their own names (see [Tool names](#tool-names)). `Tags` are free-form labels used to filter the list.

### Subprocess (stdio) servers

- `Command`: the executable to run, for example `npx` or `uvx`. It must be found on the `PATH` of the backend and worker containers, or be an absolute path to an executable file.
- `Arguments (space-separated)`: for example `-y @modelcontextprotocol/server-github`. The field is split on spaces, so a single argument cannot contain a space. A preset placeholder such as `/path/to/allowed/dir` must be replaced before the server can be created.
- `Environment Variables (secrets)`: one `Variable Name` / `Value` pair per variable the server reads, such as `GITHUB_PERSONAL_ACCESS_TOKEN`. Values are stored encrypted and are never displayed again. When you edit the server, leave the list empty to keep the stored variables; entering any variable replaces the whole stored set.

Put credentials in environment variables, never in the arguments: arguments are stored in clear and shown to everyone who can configure the server.

What the process can rely on:

- **Runtimes.** The official images ship Node.js (for `npx`) and `uv` (for `uvx`; when only `uv` is present, `uvx …` runs as `uv tool run …`). They do not include the Docker CLI, so a server started with `docker run …` needs an image you build with it, and access to a Docker daemon. The presets started this way (VulnCheck, TheHive, Elastic Security, ReversingLabs) show this warning when you pick them.
- **Environment.** The process does not inherit the platform's configuration or secrets. It starts with a minimal environment (`PATH`, `HOME`, the locale and temporary-directory variables, `PYTHON*` variables) plus the variables you configured. A server that needs a proxy or a custom CA must be given the corresponding variables explicitly; `NODE_TLS_REJECT_UNAUTHORIZED` in particular is never passed through from the platform.
- **Network.** `npx` and `uvx` download the package on first start, so the containers need access to the npm or PyPI registry (or your mirror). The first start, download included, must complete within the 60-second connection limit.
- **Isolation.** The process runs with the permissions of the backend and worker, without a sandbox. Only run servers you trust, and prefer a remote transport for anything third-party.
- **No relay.** A stdio server runs on the platform itself, so it cannot be routed through a relay group.

### Remote servers (Streamable HTTP or SSE)

- `Server URL` (or `SSE URL`): the endpoint given by the provider, for example `https://mcp.example.com/mcp`. XTM One connects to it from its backend and worker, so it must be reachable from there.
- `Route through`: `Direct (from the platform)` by default. To reach a server inside a network the platform cannot reach, choose a relay group: every request the server makes — its tools, the connection test, OAuth detection, sign-in and token refresh — then leaves from that network through one of its relays, and fails rather than falling back to a direct call when no relay is online. See [Relays](relays.md).
- `Authentication`: `Secret Headers` or `OAuth 2.0`. Presets for hosted servers default to `OAuth 2.0`.

#### Secret headers

Use this when the provider gives you a static API key. Add one `Header Name` / `Header Value` pair per header, typically `Authorization` with `Bearer <your key>`, or the provider's own header such as `X-API-Key`. Headers are stored encrypted and never displayed again. When you edit the server, leave the header list empty to keep the stored headers; entering any header replaces the whole stored set, so re-enter every header the server needs.

#### OAuth 2.0

Use this when the provider asks you to sign in. In most cases you only need the server URL:

1. Choose `OAuth 2.0` and click `Detect settings`. XTM One reads the server's published OAuth metadata (RFC 9728 protected-resource metadata, then the authorization server's RFC 8414 or OpenID Connect metadata) and fills `Authorize URL`, `Token URL` and `Registration URL (optional)`. Every discovered endpoint must use `https://` (`http://` is accepted only for `localhost`).
2. Leave `Client ID (optional)` and `Client Secret (optional)` empty unless the provider gave you a client. Without one, XTM One registers itself with the provider automatically (RFC 7591). It first requests a public client and uses PKCE; if the provider rejects that registration mode, it retries without selecting an authentication method and securely stores any client secret the provider returns.
3. Set `Scopes (space-separated)` if the provider documents required scopes.
4. Create the server, then open its `Configuration` tab and click `Authorize` in the `OAuth Connection` card. You are sent to the provider to sign in and consent, then back to the server page, which shows `OAuth connected` and discovers the tools.

Values you type by hand always take precedence over discovered ones. If nothing can be discovered, enter the `Authorize URL` and `Token URL` from the provider's documentation.

If the provider requires a pre-registered client, create an OAuth application on the provider's side and register this redirect URI:

```text
<BASE_URL>/api/oauth/callback
```

`<BASE_URL>` is the platform's public URL, including its base path when it is served under one (see [Installation](../../deployment/installation.md)). Then enter the `Client ID` and, for a confidential client, the `Client Secret` the provider issued.

!!! warning "One connection per server"
    The token belongs to the server, not to each user: everyone who uses the server calls it with the account that authorized it, just as they would share its API key. On a personal server only its owner can authorize; on a company-managed server only an administrator can, and the configuration screen says so. Choose the account you authorize with accordingly — typically a service account with the least privileges the agents need.

Tokens are refreshed automatically before they expire when the provider issued a refresh token. The `OAuth Connection` card shows `Connected`, `Error` (a refresh failed: click `Re-authorize`) or `Not connected`. Switching the server back to `Secret Headers` discards the stored tokens.

Administrators control two related settings: `MCP_OUTBOUND_DCR_ENABLED` turns automatic client registration off (every OAuth server then needs a client ID), and `OAUTH_TOKEN_URL_STRICT` restricts which token URLs are accepted. See [MCP OAuth](../../deployment/configuration.md#mcp-oauth) and [Security](../../deployment/configuration.md#security).

### Import from a client configuration

`Import` on the list page creates servers from a configuration you already have, either `From a file` (a `.json` file) or `From a URL` (`Configuration URL`, then `Fetch`). It accepts:

- the `mcpServers` object used by Claude Desktop, Cursor and similar clients;
- the `servers` object used by VS Code;
- an MCP registry `server.json`, whose packages map to `npx` (npm), `uvx` (PyPI) or `docker` (OCI).

The review step lists every server found; select the ones to create. Placeholder values in environment variables and headers are dropped and reported as `Still needs a value for …`; placeholders in command arguments are imported as written, so review and replace them before using the server. Open each imported server afterwards and fill in any missing values. Imported servers use header authentication; switch a server to `OAuth 2.0` in its configuration if it needs it. A configuration URL must use `https://` and point to a public address. A file is limited to 1 MB and 100 servers.

## Test the connection

On the server page, `Test connection` connects, lists the server's tools and saves them. It has three outcomes:

- `Connection successful`, with the number of tools discovered;
- `Authorization required`: the server refused the request or publishes OAuth metadata while no credential is set — authorize it, or switch `Authentication` to `OAuth 2.0` and authorize it;
- an error message, which usually names the cause (see [Troubleshooting](#troubleshooting)).

In the `Discovered Tools` card of the `Configuration` tab, `Discover Tools` or `Refresh Tools` lists the tools again, and `Test` on a tool runs it once with arguments you provide. Refresh after any change to the transport, URL or credentials. Testing requires the right to configure the server.

## Give an agent access

An agent only uses the MCP servers attached to it:

1. Open the agent, then `Configuration > Tooling`.
2. In `MCP Tool Servers`, click `Edit` and click a server to attach or detach it.
3. Optionally use the tool scope button (`All tools` by default) to keep only the tools the agent needs. Fewer tools means a clearer choice for the model and fewer actions it could take.

Only servers you can see are offered, and only enabled ones. A company-managed agent can only use company-managed servers. Assignment runs use the agent's servers and tool scope.

In chat, the tools panel of a conversation lets you add MCP servers to the `General Assistant` for that conversation, and your [profile](../my-profile.md) holds the servers selected by default. A server whose `Hide from chat` switch is on (edit view of its `Configuration` tab) is left out of these chat pickers but can still be attached to agents.

## Tool names

The model sees each tool under the name the server gives it, not prefixed with the server name (characters other than letters, digits, `_` and `-` are replaced with `_`, and names are cut to 64 characters). If two sources attached to the same agent or conversation expose a tool with the same name, only one of them is offered. Limit the tool scope, or avoid attaching two servers that publish the same tool names.

## Approvals, limits and files

- **Approvals.** MCP tools go through the platform's [human approval of tool calls](../../administration/ai-governance.md#human-approval-of-tool-calls): on an agent that asks before running tools, a tool asks for a person's decision first unless a rule lets it run. The read tools of company-managed, enabled servers run without asking automatically; the tools of a personal server ask unless a rule or an approval covers them.
- **Time.** Connecting (start, handshake and tool listing) is limited to 60 seconds. A tool call is limited by the run's tool timeout (300 seconds by default). After repeated connection failures the platform stops retrying a server for 5 minutes; `Test connection` always retries immediately.
- **Size.** A tool result is cut to fit the model's context: at most 15% of the model's context window, and never more than 150,000 characters. Files a tool returns are kept as files of the conversation or run. When the agent follows a linked resource with `read_mcp_resource`, that read stores at most 40 MB in total — see [Files an MCP tool returns](mcp-servers.md#files-an-mcp-tool-returns).

## Sharing and company-managed servers

A server can be shared with groups or users as `Read only` (use it, cannot edit) or `Can configure` (use and edit, test included). Sharing a server lends its credentials: whoever can use it calls the external system with your headers, environment variables or OAuth token. XTM One asks you to confirm before lending them; see [Sharing and access](sharing-and-access.md).

For a server the whole organisation relies on, prefer a company-managed server created by an administrator, authorized with a dedicated service account. `Restrict access` limits it to named people and groups when it should not reach everyone. Its read tools are also the only MCP tools that run without asking automatically on an agent that asks before running tools.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `command '…' was not found in PATH` | The command is not installed in the backend and worker images. Install it in your image, or give an absolute path to an executable. |
| `… does not exist or is not executable` | The absolute path is wrong, or the file lacks the execute permission. |
| The test times out on a stdio server | The first `npx` / `uvx` download is slow or blocked. Check the containers' access to the package registry, or pre-install the package in your image. |
| `Authorization required` | The server needs credentials: set headers, or switch to `OAuth 2.0` and click `Authorize`. On a server you do not own, its owner (or an administrator, for a company-managed server) has to authorize it. |
| `Connection test failed: the server redirects to …` | The server answered with a redirect the connection does not follow (another host, or one that changes the request). If the address it names is the intended server, save the server with that URL. |
| `Automatic OAuth client registration is disabled on this platform…` | `MCP_OUTBOUND_DCR_ENABLED` is off. Create a client at the provider (redirect URI `<BASE_URL>/api/oauth/callback`) and enter its `Client ID`. |
| `…enter the Authorize URL and Token URL in the server configuration.` | The server publishes no OAuth metadata. Copy both URLs from the provider's documentation. |
| The provider shows an error instead of its sign-in page, or `OAuth failed` after signing in | The provider rejected the client or the redirect URI. Check that the redirect URI registered at the provider is exactly `<BASE_URL>/api/oauth/callback`, and that the client ID and secret are the ones it issued. |
| OAuth status `Error` | The token could not be refreshed (revoked consent, expired refresh token). Click `Re-authorize`. |
| The server is not offered when editing an agent | It is not visible to you, or the agent is company-managed and the server is not. |
| The agent does not use an expected tool | Check the agent's tool scope for that server, `Refresh Tools` on the server, and whether another attached source exposes a tool with the same name. |

## Next step

Once the server is attached, try it from a [chat](../chat.md) with the agent, then review its calls in the server's `Activity` tab (see [MCP servers](mcp-servers.md)).
