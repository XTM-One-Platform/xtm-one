# Resources

Resources are the reusable building blocks behind agents and conversations. If agents are the assistants, resources are what those assistants know and act through. In the left navigation they sit in two groups:

- `Agent Studio` holds what defines your agents: `Create an agent`, `Knowledge`, `Prompts`, `Templates`, and `Skills`.
- `Tools & Integrations` holds what your agents act through: `MCP Servers`, `Tools`, `Integrations`, `Browser profiles`, `Variables`, and `Relays`.

Click a group's name to open its overview page, with one card per area. You usually come here when you are preparing an agent, improving an existing setup, or checking how a workflow gets its data and actions.

## Agent Studio

- [Knowledge bases](knowledge-bases.md) store documents and indexed content that agents can search, so answers are grounded in trusted material. Content can be uploaded directly or kept in sync from external sources such as websites, Notion, cloud storage, and GitHub.
- [Prompts](prompts.md) are reusable text instructions that keep wording consistent across chat requests, agent behavior, and recurring workflows.
- [Templates](templates.md) hold the brand behind generated presentations, reports and dashboards — palette, typefaces, layouts, logos and composition guidance — so the same request produces on-brand output for everyone. A read-only brand template, the `XTM One Brand Deck`, ships with every platform.
- [Skills](skills.md) are reusable instruction sets — a packaged behavior or method — that can be attached to agents. A skill can be authored directly or fetched from a source URL.

To build an agent from these pieces, see [Create an agent](../create-agent.md).

## Tools & Integrations

- [MCP servers](mcp-servers.md) expose external tools to XTM One in a controlled way. Use them when agents need capabilities such as search, data lookups, or system actions provided through MCP. See also [Connect an external MCP server](connect-mcp-server.md) and [MCP catalogs](mcp-catalogs.md).
- [Tools](tools.md) are the actions agents can use directly: the built-in tools (files, web, browser, design, the platform tool and others). Custom tools are off by default in XTM One; an administrator can turn them on.
- [Integrations](integrations.md) connect XTM One to external services such as email, ticketing, messaging, and cloud document systems, giving agents live access to those systems.
- [Browser profiles](browser-profiles.md) are saved sign-ins to web applications: an agent given one opens the application signed in with its `Browser` tool, without seeing the credentials.
- [Variables](variables.md) store reusable values that resolve at runtime in prompts, personas, skills, and tools, so key values are not repeated by hand. Values can be marked `Secret`. Variables are off by default in XTM One; an administrator can turn them on.
- [Relays](relays.md) let XTM One reach services inside a network it cannot reach directly, without opening an inbound flow toward that network.

## Other related pages

- [XTM One Desktop and runners](runners.md): the native app, and the machines your agents can act on.
- [Agent-to-agent (A2A)](agent-to-agent.md): how your agents work with agents outside XTM One.
- [Sharing and access](sharing-and-access.md): who can use what you lend, and through what.
- [Platform MCP servers](platform-mcp-servers.md) and [Platform tools MCP server](platform-tools-mcp-server.md): XTM One as an MCP server for your own clients.

## Grid and list views

Every resource list page — and the `Agents` page — can be displayed two ways. The toggle sits at the right of the filter row, next to the item count.

- **Grid** is the default: one card per item, optimized for browsing and recognizing things at a glance.
- **List** shows the same items as table rows. It fits far more per screen and exposes extra attributes side by side, so you can compare a whole fleet at once — which model each agent runs, which tools are enabled, how many documents each knowledge base holds.

On the `Agents` page, favorites (the star on a card or row) sit above every other item in both views — sorting a column reorders the favorites among themselves rather than sending them back into the pile.

In list view, click a column header to sort by it; click again to reverse the order. Items with no value for that column (no model pinned, never fetched) always stay at the bottom, in both directions. Every action available on a card — opening the item, enabling or disabling it, starting a chat, copying a variable reference — is available on the matching row.

Your choice is remembered per page and across sessions, so a page you prefer as a table stays a table the next time you sign in.

## Platform MCP servers

XTM One is not only an MCP client — it is also an MCP **server** for the connected Filigran platforms. As soon as an OpenCTI, OpenAEV, or OpenCRQ instance is registered with XTM One, external MCP clients (Cursor, Claude Desktop, and others) can connect to `/mcp/opencti`, `/mcp/openaev`, and `/mcp/opencrq` with a personal API key and work with threat intelligence, exposure validation, and governance-risk-compliance content directly.

See [`Platform MCP servers`](platform-mcp-servers.md) for endpoints, authentication, the tool surface, and client configuration.

## Platform tools MCP server

XTM One also exposes **itself** over MCP, on `/mcp/xtm`: agent traces, agents, assignments, runs, knowledge bases, prompts, skills, custom tools, integrations, and variables — scoped to what the calling user can already see and manage. It is how you read an agent's traces from Claude Desktop or Cursor and fix its configuration without leaving the client. Off by default; an administrator enables it in `Settings > MCP Endpoint`.

See [`Platform tools MCP server`](platform-tools-mcp-server.md).

## How the resource types work together

In a typical workflow:

- an integration connects to the outside system
- a knowledge base provides trusted context
- a prompt or skill shapes the behavior
- a tool performs an action
- a variable avoids repeating key values

This is why resources matter so much: they are where assistants become usable and consistent.

## Next step

Continue with the detailed guide of the resource type you need, starting with [Integrations](integrations.md).
