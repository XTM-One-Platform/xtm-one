# MCP servers

This page gives you a quick tour of the MCP server screens. To configure a new server — transport, credentials, OAuth, testing and attaching it to an agent — see [Connect an external MCP server](connect-mcp-server.md).

## The list page

In the left navigation, open `Tools & Integrations > MCP Servers` — the first entry of the group, because an MCP server is the richest capability an agent acts through (a whole tool server rather than one function). The page also carries the MCP endpoints this platform itself exposes; see [Platform MCP servers](platform-mcp-servers.md) and [Platform tools MCP server](platform-tools-mcp-server.md). The list page is built for quick scanning: search by name, command, or URL, and filter by visibility or tags. Each card answers two questions at a glance — is the server likely usable right now (enabled or disabled, transport type), and what kind of setup sits behind it (subprocess vs. HTTP, whether OAuth or environment variables are involved) if something goes wrong later. If you have permission, `Add Server` connects a new external toolset, and `Import` creates servers from a client configuration you already have (see [Import from a client configuration](connect-mcp-server.md#import-from-a-client-configuration)).

## The detail page

Opening a server shows a header with its name, transport, enabled state, and OAuth status when relevant. `Test connection` is the main action — try it first after any setup or credential change, because it tells you whether the problem is the server connection itself or something later in the agent workflow. The page is organized into `Overview`, `Configuration`, and `Activity` tabs.

### Overview

`Overview` shows whether the server is meaningfully ready: how many tools it discovered, how often they are called, its connection details, and which agents depend on it.

### Configuration

`Configuration` is the most operationally important screen. It covers the server's identity and connection (transport, command or URL, environment variables or headers) and its visibility and sharing. You can switch transport directly here between subprocess (stdio), Streamable HTTP, and SSE.

The URL of a Streamable HTTP or SSE server must be an `http` or `https` URL. The platform connects to it from its own network, so it checks the address before every connection - and when you save it - against the **MCP server URL policy** an administrator sets in `Settings > Access and security`:

- By default (`Permissive`) only cloud metadata addresses (such as `169.254.169.254`) are refused; a server on `localhost` or on a private network works.
- `Private networks` also refuses loopback (`localhost`, `127.0.0.1`, `::1`), link-local and other reserved addresses. A server on a private network (`10.x`, `172.16-31.x`, `192.168.x`) still works.
- `Public only` refuses every internal address. Reach a server inside your network through a [relay group](relays.md) (`Route through`) instead.

A redirect is checked the same way, and so are the OAuth discovery requests the platform makes to connect an OAuth server. The connection follows a redirect only within the server's own origin (a trailing slash, `http` to `https` on the same host) and only when it keeps the request: a server that redirects anywhere else (to `www.`, to another host, with a `301` / `302` on a request that posts) fails the test with a message naming the address it points to, without its query. If that is the intended server, save it with that URL. An address the policy refuses gives the message "This MCP server URL is not allowed": when the server is one you trust, ask an administrator to list its origin in `Trusted MCP server origins`. See `MCP_SERVER_URL_POLICY` in [Configuration](../../deployment/configuration.md#security) for the full rules.

The arguments field reads like a command line: arguments are separated by spaces, and an argument that holds a space is written in double quotes (`--header "Authorization: Bearer ..."`), where `\"` and `\\` stand for a quote and a backslash. An argument with a space is shown that way too, so it stays one argument when you save.

A credential written into the arguments or a URL is shown masked to everyone who can see the server, its owner included: a token keeps its first characters (`ghp_ab***`), and the value of a secret-named flag or variable (`--token ***`, `GITHUB_TOKEN=***`), a URL's password or secret query value, and a `Bearer` token become `***`. Saving leaves a masked value as stored, wherever it now sits in the arguments, so you can rename, tag, share or add an argument without retyping it. To change a credential, type its full value. A save is refused, with nothing changed, when a masked value no longer matches one stored value: a `***` moved after another flag, a URL edited around its masked password, or one of two tokens that look the same once masked removed. Type the full value of each masked argument or URL then. The full values are in the server's export, for the people who can edit it.

The same screen holds the discovered tools card, where you discover or refresh the server's tools, read each tool's description, and test an individual tool. This matters because most MCP questions are not about the server object itself — they are about whether the expected tool is actually visible and callable. When the server uses OAuth, an OAuth connection card shows whether it is configured but not yet authenticated, with an authorize or re-authorize action. On a company-managed server that action is admin-only, because the resulting connection is a single shared one — every user of the platform calls the server through the token the admin authorized, just as they all share its API key when it authenticates with headers.

Once authorized, the connection stays usable without re-authorizing, even when the authorization server issues short-lived access tokens (a few minutes). The platform refreshes the token before it expires, refreshes it again when a connection finds it about to expire, and replaces it once when the server refuses it. For this to work, the authorization server must issue a refresh token. When its metadata lists `offline_access` (Okta, Entra ID and Auth0 issue refresh tokens only on request), `Authorize` adds that scope to the server's scopes, before it registers a client for the platform. A server authorized before this change gets it the next time someone clicks `Re-authorize`, unless the platform registered its client automatically without that scope: clear the client ID in the configuration, then click `Re-authorize` to register a new client that asks for it. If the server still asks for authorization a few minutes after each connection, its authorization server issues no refresh token: check that the client is allowed the refresh token grant.

### Activity

`Activity` is the traceable history — whether the server changed recently, whether calls were made, and whether an issue started after an edit.

## Files an MCP tool returns

A file an MCP tool sends back (an image, a document, an embedded resource) is kept as a file of the conversation or run: the agent reads it, you get a download card, and the agent can hand it to any other tool that takes a file. A tool may instead return a link to a resource without its content, often because it is large. The agent sees the link, and when it needs the file it asks the same MCP server for it (`read_mcp_resource`), over the server's own connection: the file is then kept like any other. Only a link the server's own tools returned in the run can be read this way, up to the platform's 40 MB file limit.

## Shared, company, and read-only states

Ownership rules shape these pages: private servers are editable by their owner, shared servers are reused across group members, and company-managed servers are visible more widely but read-only for non-admins. Testing a server (`Test connection`, `Discover Tools`, a tool's `Test`) requires the same right as editing it: a `Read only` share lets you use the server from your agents, not test it.

## Good habits

- Use `Test connection` before troubleshooting the agent itself.
- Refresh discovered tools after changing transport or credentials.
- Read the discovered tool list, not just the server name.
- Notice the difference between a configured server and an OAuth-connected one.
