# MCP catalogs

XTM One publishes its MCP servers in the official **MCP Registry**, the catalog that VS Code, the GitHub MCP registry and other MCP clients read. You can install a server from your client's catalog instead of copying its address by hand.

!!! note "Availability"
    The catalog listing is rolling out with an upcoming release. Until XTM One appears in your client's catalog, add a server by its address as a custom connector ([Add XTM One to Claude](#add-xtm-one-to-claude)) or with an API key ([Use an API key instead](#use-an-api-key-instead)).

## What is listed

| Catalog entry | Endpoint | What it gives you |
| --- | --- | --- |
| XTM One platform tools | `/mcp/xtm` | XTM One's own tools: agents, assignments, runs, traces, knowledge bases, prompts and skills. See [Platform tools MCP server](platform-tools-mcp-server.md). |
| XTM One agents | `/mcp/user` | The agents you selected in `My Profile` > `MCP Endpoint` or, if you selected none, every agent you can chat with; each one is a tool that runs the agent on your request. An API key limited to some agents exposes those only. |
| OpenCTI (via XTM One) | `/mcp/opencti` | The OpenCTI tools of the [platform MCP servers](platform-mcp-servers.md). |
| OpenAEV (via XTM One) | `/mcp/openaev` | The OpenAEV tools of the platform MCP servers. |
| OpenCRQ (via XTM One) | `/mcp/opencrq` | The OpenCRQ tools of the platform MCP servers. |

Every entry works with your own XTM One instance: the catalog does not host anything, it tells your client how to reach your instance.

The global agent endpoint (`/mcp/global`) is not listed: it signs in with an administrator key for the whole instance rather than with your account.

## Before you start

Check with your administrator that what you want to install is available on your instance:

- **XTM One platform tools**: the server is turned on in `Settings` > `MCP Endpoint`. It is off by default.
- **OpenCTI, OpenAEV, OpenCRQ**: the platform is connected to XTM One, and the platform MCP servers are on in `Settings` > `MCP Endpoint`.
- **XTM One agents**: the instance runs Enterprise Edition, and the agents you want are enabled and available in chat.
- **All of them**: AI is turned on.

## Install from a catalog

1. Search your client's MCP catalog for `XTM One`, `OpenCTI`, `OpenAEV` or `OpenCRQ`.
2. Install the entry. Your client asks for the **host of your XTM One instance**: enter its host name, for example `xtm.example.com`. If your instance is served under a path, include it, for example `example.com/xtm-one`. A client that installs several XTM One entries can reuse the same answer.
3. Your client opens an XTM One page in your browser. Sign in, check the name of the client, and approve it.

The page names the client and the site it comes from, for example `Claude Code (claude.ai)`. Approve only a client you are installing yourself.

Every tool call then runs as you, with your permissions. The tools carry standard annotations, so your client knows which calls only read and which change or delete something, and most clients ask you before a destructive call.

## Add XTM One to Claude

Until XTM One is listed in Claude's own connector directory, add a server to Claude as a custom connector: in Claude, open `Settings` > `Connectors`, choose `Add custom connector`, and paste the address of the endpoint, for example `https://xtm.example.com/mcp/xtm`. Claude opens the same XTM One sign-in page.

!!! note
    claude.ai connects from Anthropic's servers, not from your computer. Your XTM One instance must be reachable from the internet for a custom connector added in claude.ai, Claude Desktop or Claude mobile. Claude Code runs on your computer and only needs to reach your instance from there.

## Use an API key instead

A client that does not support OAuth connects with a personal API key (`fcp-...`), created in `My Profile` > `API Keys` and sent as a bearer token. The configuration snippets in `My Profile` > `MCP Endpoint` already carry the right address.

## Troubleshooting

- **The client says the server does not exist (404)**: the platform behind it (OpenCTI, OpenAEV, OpenCRQ) is not connected to your instance.
- **The client is refused (403)**: the server is turned off, AI is off, or, for the agents server, the instance does not run Enterprise Edition. Ask your administrator.
- **No browser page opens**: your client does not support OAuth sign-in. Use an API key.
- **The sign-in page refuses the client**: an administrator may have turned off client self-registration or client metadata documents in `Settings` > `Security`. Use an API key, or ask your administrator.
- **A web-based client is refused (403) before it signs in**: XTM One only answers web pages served from its own address or from an address your administrator allows.

## Next step

Explore what you can do with the [platform tools MCP server](platform-tools-mcp-server.md).
