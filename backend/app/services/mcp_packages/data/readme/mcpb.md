# {{PLATFORM_NAME}} platform tools — Claude Desktop extension

Gives Claude Desktop the {{PLATFORM_NAME}} platform tools: agent traces,
agents, assignments, runs, knowledge bases, prompts, skills, custom tools,
integrations and variables.

{{INSTALL_AUTH}}

## How it works

`.mcpb` extensions run a **local** server over stdio, but the
{{PLATFORM_NAME}} server is remote and speaks MCP Streamable HTTP. This bundle
therefore carries a small bridge (`server/index.js`) that forwards each
JSON-RPC message over HTTPS with your key in the `Authorization` header.

It is roughly a hundred lines, has no dependencies, and fetches nothing at
runtime — read it before you install if you would rather check than trust.
It requires the Node.js runtime that ships with Claude Desktop (18 or newer).

If you prefer no local process at all, add the endpoint as a **custom
connector** instead: copy the URL from **My Profile > MCP Endpoint** and paste
it into Claude Desktop's connector settings. You lose the bundled skill, which
you can add separately from `skills/xtm-one-platform-tools/SKILL.md`.

## The skill

`skills/xtm-one-platform-tools/` holds the guidance for these tools, including
a reference generated from the tool surface this server offered when you
downloaded the package. Claude Desktop does not read a `skills/` directory
from an extension, so add it as a skill yourself if you want the model to have
it.

## Troubleshooting

The bridge turns transport failures into readable errors rather than hanging,
so a failing call says why:

- `401` — the key is wrong, revoked, or inactive.
- `403` — an administrator has not enabled the server in
  **Settings > Integrations > MCP Endpoint**, or AI is turned off on the deployment.
- `Cannot reach …` — the URL is unreachable from this machine (VPN, firewall,
  or a private deployment address).
