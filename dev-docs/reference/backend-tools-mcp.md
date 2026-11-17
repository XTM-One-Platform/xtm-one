# MCP servers in the agentic loop

How MCP servers are connected for a run, how their binary results and resource links are handled, the `MCPServer` model, its API and common servers; the rules of this area live in `backend/app/tools/AGENTS.md`.

## 4. MCP Server Lifecycle

MCP connections live for one run (an assignment run, a chat turn) and are closed afterwards:

1. The run's entry point (`assignment_engine`, `agent_chat`, `general_chat`) picks the servers: for an assignment, its `tool_sources`, else the agent's `mcp_server_ids`.
2. `services/builtin_tools/loaders.load_mcp_tools()` loads the enabled servers, decrypts env vars / headers / OAuth tokens (stored Fernet-encrypted or as vault references, `api/v1/helpers.build_mcp_runtime`), and registers their tools on the run's `CompositeToolExecutor` through an `MCPManager`, from the server's cached tool list when it has one (the connection is then opened on the first call), applying the agent's per-server tool scoping (`mcp_allowed_tools`).
3. A remote server (`sse` / `streamable_http`) is reached only where the MCP server URL policy allows (`tools/mcp_destinations.py`), directly or through its relay group (`relay_group_id`).
4. After the run, `cleanup_mcp_manager()` calls `mcp_manager.disconnect_all()` in a `finally`.

A failed connection attempt raises its failure from `MCPConnection.connect()`, never a cancellation the caller did not ask for. The first request of a `streamable_http` connection runs in a task of the MCP SDK's task group, so its failure (a closed port, a 401, a 500) reaches `initialize()` as the cancellation the group delivers to the connecting task; `connect()` closes the transport, takes the failure the group raises on close, withdraws that cancellation and reports the failure like any other: 401 detection, the unreachable-server WARNING and failure count, then the raise. `test_connection` (the `/test` route) then answers `error` or `auth_required` and `/test-tool` its `error`, as over `sse`, and the caller's next await (the route's database write) is not cancelled. A cancellation the task carried before the attempt, or one with no transport failure behind it, still propagates.

A remote transport follows a redirect only within the endpoint's origin and only when it keeps the request method (mcp 1.30, `stream_within_origin`); the client's `follow_redirects` is not consulted, so `mcp_http_client_factory` sets none. Any other redirect fails the connection with an `httpx.HTTPStatusError` for the redirect response; `_report_connect_failure()` finds it in the failure (`_unfollowed_redirect()`), logs a WARNING without a traceback naming the target, counts it like an unreachable server and raises `MCPRedirectNotFollowedError`. Its `location` is the SDK's `redirect_location()` (no userinfo, query or fragment) through `redact_secret_text()`, cut to 300 characters. `test_connection` answers it as `error` with `redirect_location` and runs no authorization probe; the `/test` route names that location in a fixed sentence, and a lazy tool call's connection-failure result carries the error's message. `probe_auth_challenge()` sends through the SDK's `request_within_origin()`, so it never reports a 401 from a host the transport would not reach.

A binary tool result (an image, a PDF, a CSV) is persisted through `persist_tool_artifact(source="mcp")` under the manager's `MCPArtifactScope`: the user / conversation / assignment run the run's `generate_file` uses, handed over as `load_mcp_tools(..., artifact_scope=...)`, so the next turn's `FileContext`, the run and a HITL resume all find it. The public widget scopes it to the widget conversation and no user: only that session downloads it, through `GET /public/chat/{web_token}/sessions/{session_id}/files/{file_id}`. The MCP server page's "Test tool" call scopes it to the tester (their own file, listed in their Files). A manager that only lists tools (connection test, tool refresh, OAuth tool discovery) never persists one; one built without a scope would persist an unscoped row.

A `resource_link` block carries no content: the result shows it as a `[Resource link: ...]` line naming its URI, its server and that server's id (`MCPServerRuntime.failure_key`: names are not unique), and the connection records the link. `read_mcp_resource` (`MCPManager.resource_tools()`, offered by `load_mcp_tools` and the public widget next to the servers' own tools, source `mcp:resources`; a reserved name, dispatched before any server tool) reads ONE such link on demand: `resources/read` on the connection whose tool linked it, over that session and its credentials, never a URI no tool of the run linked and never a fetch by the platform; bounded by the link's declared size and the stored-file limit (`MAX_UPLOAD_SIZE_BYTES`, also across contents), `_RESOURCE_READ_TIMEOUT` and `_MAX_RESOURCE_CONTENTS`; each content stored like an embedded resource (`_embedded_resource_part`: text redacted, blob decoded, the link's MIME type when the content declares none) and carried on the result. Those bounds are on what is STORED: the bytes received are the transport's, which parses every message of a session (a tool result, a notification the server sends unasked) before the session sees it. On demand, not automatic: a result may list many links to large files, and every file on a result is read into the context.

## MCP Server DB Model

`MCPServer` in `app/models/mcp_server.py` (main columns; it also carries the visibility columns of a shareable entity):

- `name`: human-readable label
- `is_company_managed`: shared with all users if true (admin toggle)
- `transport`: "stdio", "sse", or "streamable_http"
- `command`, `args`: for stdio (e.g., `npx`, `["-y", "@modelcontextprotocol/server-github"]`)
- `env_enc`: encrypted JSON blob of env vars (e.g., `{"GITHUB_TOKEN": "ghp_..."}`)
- `url`, `headers_enc`: for SSE/streamable_http transport
- `auth_mode`: `headers` (default) or `oauth`, with the OAuth client and token columns (`client_id_enc`, `access_token_enc`, ...)
- `relay_group_id`: the relay group a remote transport connects through (NULL = direct)
- `discovered_tools`: the cached tool list
- `enabled`: toggle on/off

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/mcp-servers` | List the MCP servers the user can see |
| POST | `/api/v1/mcp-servers` | Create a new MCP server |
| GET | `/api/v1/mcp-servers/{id}` | Get one MCP server |
| PUT | `/api/v1/mcp-servers/{id}` | Update an MCP server |
| DELETE | `/api/v1/mcp-servers/{id}` | Delete an MCP server |
| POST | `/api/v1/mcp-servers/{id}/test` | Test connectivity, return discovered tools |
| POST | `/api/v1/mcp-servers/{id}/test-tool` | Call one tool (the "Test tool" dialog) |
| POST | `/api/v1/mcp-servers/{id}/refresh-tools` | Re-discover and cache the tool list |
| POST / GET | `/api/v1/mcp-servers/oauth/discover`, `/{id}/authorize` | OAuth discovery and authorization |
| GET / POST | `/api/v1/mcp-servers/{id}/export`, `/import`, `/import/config/preview`, `/import/config` | Export and import |

## Common MCP Servers

| Server | npm Package | Env Var |
|---|---|---|
| GitHub | `@modelcontextprotocol/server-github` | `GITHUB_PERSONAL_ACCESS_TOKEN` |
| Filesystem | `@modelcontextprotocol/server-filesystem` | — (pass path as arg) |
| PostgreSQL | `@modelcontextprotocol/server-postgres` | `POSTGRES_CONNECTION_STRING` |
| Slack | `@modelcontextprotocol/server-slack` | `SLACK_BOT_TOKEN` |
| Google Drive | `@modelcontextprotocol/server-gdrive` | OAuth |
| Brave Search | `@modelcontextprotocol/server-brave-search` | `BRAVE_API_KEY` |

Browse more at: https://github.com/modelcontextprotocol/servers
