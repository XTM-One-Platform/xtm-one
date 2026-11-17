# Platform tools MCP server

Alongside the [platform MCP servers](platform-mcp-servers.md) — which bridge the *connected* OpenCTI, OpenAEV, and OpenCRQ instances — XTM One exposes **itself** over MCP.

One endpoint, `/mcp/xtm`, lets any MCP-compatible client (Claude Desktop, Cursor, Claude Code, your own agents) read your **agent traces** and manage the objects you work with every day: agents, assignments, runs, knowledge bases, prompts, skills, custom tools, integrations, MCP servers, variables, and [workspaces](../workspaces.md) (with the conversations you file in them).

The typical use: an agent misbehaved, so you read its traces from your client, spot the step that failed, and fix the agent's configuration — without leaving the client.

## What you get

| Endpoint | What it exposes |
| --- | --- |
| `https://<your-xtm-one>/mcp/xtm` | XTM One's own tools: agent traces, agents, assignments, runs, knowledge bases (including semantic search), prompts, skills, custom tools, integrations, MCP servers, variables, workspaces |

The transport is MCP **Streamable HTTP**, the same as every other XTM One MCP endpoint, in the protocol revisions 2026-07-28, 2025-11-25, 2025-06-18 and 2025-03-26.

### Observability

- `inspect_agent_traces` — recent traces, filterable by period, status, source, and model. Naming a `target_agent_id` scopes to one agent; **omitting it lists traces across agents**, which is how you find *which* agent failed before drilling into it. With `include_spans` (the default), each trace carries a `rollup` computed from all its spans: iterations, model calls, tool calls and tool errors, tokens and cost, per-tool calls and result size, and the five largest tool results with their span id, so you can see where a run spent its tokens and read any of those results with `get_trace_payload`. A run that stored a rollup when it ended also carries it, in every mode (without spans too): wall time, time in model calls, in tools and in `wait`, cache-read and cache-write tokens, and the rollup's own cost (`overhead_ms`); the listing then adds `rollup_percentiles`, the p50 and p90 of each figure over the runs it lists.
- `get_trace_payload` — the full stored value of any trace or span field. Listings return bounded previews; this reads the rest, one page at a time. A model's reasoning shows as `[reasoning omitted]`, here and in listings: read back into a prompt, Claude may decline it. Pass `include_reasoning: true` to read it, paging from offset 0.

Trace access follows the same rule as the Trace Explorer in the UI: the cross-agent listing is an administrator surface, and a non-admin reads the agents they can manage. A deleted agent's traces stay until the retention period removes them: name its id as `target_agent_id` to read the runs of it you made, or all of them if it was yours; a query matches its last name.

Every `inspect_agent_traces` listing, and every `get_trace_payload` read of a trace that is not the calling agent's own, is recorded in the audit log (`inspect_traces`). The same read by the same person - a listing of the same agents, or the same field of the same trace - is recorded once every 15 minutes: paging through one payload or changing a listing's filters writes one entry, and the entry says so in `window_seconds`. A listing of other agents, another trace or another field is recorded at once.

### Management

Everything the REST API and the UI offer for the objects you own or share:

- **Agents** — list, read, create, update, delete, plus templates and the available-resources catalog
- **Assignments and runs** — list, read, create, update, trigger, and read execution history
- **Knowledge bases** — list, read, create, update, delete, add and read documents, revise a document (with automatic re-embedding), and `search_knowledge` for semantic search across everything you can read
- **Prompts, skills, variables** — full CRUD, plus `load_skill` / `read_skill_resource` to open a skill's bundled files
- **Custom tools, integrations, MCP servers** — full CRUD

Every tool comes with a title and the standard MCP annotations: reads (`list_*`, `get_*`, `inspect_agent_traces`, `search_knowledge`) are marked read-only, `create_*` are marked not destructive, and `update_*`, `delete_*` and `trigger_assignment` (which starts an agent run) are marked destructive, which most clients ask you to confirm. These are hints for your client; your permissions decide what a call may do.

## Availability

Off by default. Unlike the platform MCP servers, this one exposes *this instance's own* configuration surface, so an administrator has to enable it explicitly:

`Settings > MCP Endpoint > Platform Tools MCP Server`

While it is disabled, the endpoint answers `403` with a message saying so.

It also needs agentic AI to be licensed (Enterprise Edition). Its tools create and change agents, assignments, flows and knowledge bases, which the REST API only allows on a licensed deployment, so on Community Edition the endpoint answers `403` with `detail.code` set to `license_required`. With AI turned off, it answers `403` with `detail.code` set to `ai_disabled`. Unlike this server, the [platform MCP servers](platform-mcp-servers.md) stay available on Community Edition.

## Permissions

The tool surface is resolved **per request, from the calling user**. There is no separate permission model to configure: every tool enforces the same three-tier gates as the REST API and the UI.

- Built-in objects are read-only for everyone.
- Company-managed objects are read-only for non-administrators.
- Group-shared objects need a `config`-tier grant to modify.
- Knowledge bases you can read are searchable; only the ones you can manage are writable.

A caller sees and changes exactly what they could see and change in the interface — never more.

## Where to find your endpoint

Open `My Profile > MCP Endpoint`. Once an administrator has enabled the server, a **Platform Tools MCP Server** card shows the endpoint URL, a copy button, and — under **Install in your client** — a downloadable package for each supported client.

## Install with a package

Three formats, all carrying the same skill so the model knows how to use the tools rather than guessing:

| Package | For | How you install it |
| --- | --- | --- |
| Claude Code plugin (`.zip`) | Claude Code | `claude --plugin-dir ./xtm-one-platform`, or add it as a local marketplace to keep it across sessions |
| Claude Desktop extension (`.mcpb`) | Claude Desktop | Double-click the file, or drag it into the Claude Desktop window |
| Agent Plugins package (`.zip`) | Any [Agent Plugins](https://agent-plugins.org) v1.0.0 client | Unzip it and point your client at the directory |

Each package already contains the endpoint URL, and the skill inside carries a tool reference **generated from the surface that server offered you** — so it lists the tools your permissions actually reach, not a maintained-by-hand list.

### One package, one server

The same three formats also package the [platform MCP servers](platform-mcp-servers.md) — OpenCTI, OpenAEV, OpenCRQ — for the platforms actually registered with XTM One. When more than one server is live, clicking a format asks which one you want; with only one there is nothing to ask and the download starts.

There is deliberately no "all of them" package. A client loads every tool definition of every server it connects to, and the four servers together currently come to more than two hundred tools — well past the point where tool selection degrades and every request carries the weight. Installing a second package stays available; it is your explicit choice rather than a default.

A bridge package ships the generated catalogue of that platform's tools and no hand-written guidance: those tools belong to OpenCTI, OpenAEV and OpenCRQ, whose own documentation is the authority on what they mean, and invented prose here would rot unnoticed as those products move.

### Other clients

The **Other** button covers the clients that install by editing a file rather than by opening a package:

| Client | File | Notes |
| --- | --- | --- |
| Codex CLI, Codex IDE extension, ChatGPT desktop app | `~/.codex/config.toml` | One file for all three — they share this configuration |
| Cursor | `~/.cursor/mcp.json` | Also accepts `.cursor/mcp.json` per project |
| VS Code | — | Needs no snippet: it reads the **Agent Plugins** package directly, so download that one |

Those two files already hold your other MCP servers, so what you get is a **fragment to merge**, not a file to replace — shown with a copy button and the path to paste it into. The one-click choice applies here too: with a key, it is written into the fragment; without, the fragment points at a `XTM_ONE_API_KEY` environment variable instead, which is what both clients document for keeping a token out of a file that often ends up in a dotfiles repository.

!!! warning "ChatGPT desktop, outside Codex"

    ChatGPT's own Developer mode (Settings → Apps) takes a **public HTTPS URL** and authenticates with OAuth or not at all — it accepts no custom header, so an API key has no place there and `http://localhost` will not connect. `/mcp/xtm` publishes OAuth 2.1 Protected Resource Metadata, so that route works on an HTTPS deployment. The `~/.codex/config.toml` entry above is what covers the ChatGPT desktop app locally.

### One-click install

The **One-click install** switch (on by default) makes the download self-contained: the platform creates a **new API key** dedicated to that package, names it after the client, and writes it into the configuration files. Install it and you are done — nothing to paste.

The trade-off is stated plainly because it is real: **the downloaded file is then a live credential.** Do not commit or share it, delete it once installed, and revoke the key from `My Profile > API Keys` if it goes anywhere it should not. It is a key of its own, so revoking it affects nothing else. It is also scoped to the MCP endpoints only - it cannot call the REST API or the LLM proxy - and expires within the maximum lifetime your administrator set, if any (see [API Keys tab](../my-profile.md#api-keys-tab)).

A new key is created rather than reused because there is nothing to reuse: keys are stored as bcrypt hashes and the plaintext exists only at creation, so no existing key can be read back into a package.

Turn the switch off and the packages carry no credential — your client asks for the key at install time and keeps it in its own secure storage. If you have no API key at all, the card says so and points you at the `API Keys` tab.

### The Agent Plugins package never carries a key

Agent Plugins 1.0.0 states that configured headers are literal, visible package data that must not contain credentials, and defines no portable credential-reference field. Embedding a key would make the package non-conformant — which is the one property that format exists for. It therefore always downloads credential-free, whatever the switch says, and its README explains where to add the bearer token in your own client.

### Why the Claude Desktop bundle contains a script

The `.mcpb` format runs a **local** MCP server over stdio, but this server is remote and speaks Streamable HTTP. The bundle therefore carries a small bridge (`server/index.js`) that forwards each JSON-RPC message over HTTPS. It has no dependencies, fetches nothing at runtime, and is about a hundred readable lines — check it before installing if you would rather verify than trust.

If you want no local process at all, add the endpoint as a Claude Desktop **custom connector** instead, using the URL from the same card.

## Authentication

A standard bearer token:

- a **personal API key** (`fcp-...`), created in `My Profile > API Keys`, or
- an **OAuth 2.1** access token — clients that support OAuth discover the flow automatically (RFC 9728), and the token is audience-bound to `/mcp/xtm`, so a token minted for another endpoint is refused here. Clients such as claude.ai, Claude Code and VS Code identify themselves with a client metadata document and need no registration.

## Configure a client by hand

For a client with no package of its own — Cursor, an internal tool, your own agent — configure it directly:

```json
{
  "mcpServers": {
    "xtm-one-platform": {
      "url": "https://<your-xtm-one>/mcp/xtm",
      "headers": {
        "Authorization": "Bearer fcp-..."
      }
    }
  }
}
```

Where to paste it:

- **Cursor**: `.cursor/mcp.json` in your project (or the global MCP settings)
- **Claude Desktop**: `claude_desktop_config.json`
- **Other clients**: any client supporting MCP Streamable HTTP with custom headers

Verify from a terminal:

```bash
curl -s -X POST https://<your-xtm-one>/mcp/xtm \
  -H "Authorization: Bearer fcp-..." \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}'
```

## Troubleshooting

| Symptom | Meaning | What to do |
| --- | --- | --- |
| `401 Unauthorized` | Missing or invalid token | Check the `Authorization` header and that your API key is active. A one-click package whose key you later revoked will fail this way — download it again |
| `403 Forbidden` | The server is disabled, AI is turned off on this deployment (`ai_disabled`), or agentic AI is not licensed (`license_required`) | Ask an administrator to enable it in `Settings > MCP Endpoint`, turn AI on, or activate Enterprise Edition, as the message says |
| A tool is missing from `tools/list` | You have nothing in scope for it | `search_knowledge` only appears when you can read at least one knowledge base; `load_skill` only when at least one skill is visible to you |
| A trace listing is refused | The cross-agent listing needs administrator rights | Name a `target_agent_id` for an agent you manage |

## Good practices

- Create a dedicated API key per MCP client so you can revoke access independently — revoking a user's keys cuts that client off immediately. The one-click packages already do this: each download gets its own key, named after the client.
- Delete a one-click package file once it is installed. The client has stored what it needs, and the file is a live credential until you do.
- Remember that write tools change real platform objects. Review what your client proposes before letting it run them.
- Leave the server disabled on deployments where no one needs it; it is a configuration surface, not a read-only report.

## Next step

To expose XTM One **agents** as tools (rather than platform management), see the `MCP Endpoint` tab described in [`My Profile`](../my-profile.md). For the connected OpenCTI / OpenAEV / OpenCRQ platforms, see [`Platform MCP servers`](platform-mcp-servers.md).
