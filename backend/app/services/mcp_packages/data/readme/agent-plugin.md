# {{PLATFORM_NAME}} platform tools — Agent Plugins package

An [Agent Plugins](https://agent-plugins.org) v1.0.0 package: the
vendor-neutral shape, for any client that implements the standard.

```
xtm-one-platform/
├── plugin.json     # manifest, targeting the 1.0.0 schema
├── mcp.json        # the /mcp/xtm Streamable HTTP server
├── skills/
│   └── xtm-one-platform-tools/
│       ├── SKILL.md
│       └── references/tool-reference.md
└── README.md
```

## Install

Unzip it and point your client at the resulting `xtm-one-platform/`
directory. The specification packages plugins as directories, not archives —
the ZIP is only the transport.

## Authentication is yours to supply

`mcp.json` declares the endpoint and nothing else. Agent Plugins 1.0.0 states
that configured headers are literal, visible package data that must not carry
credentials, and defines no portable credential-reference field, so there is
deliberately **no** `Authorization` header here.

Add one in your client's own MCP configuration:

```
Authorization: Bearer <your-api-key>
```

Create the key in **My Profile > API Keys** on {{PLATFORM_NAME}}. Every call
runs with your identity and your permissions. Use a dedicated key per client
so you can revoke it on its own.

OAuth 2.1 also works if your client supports it: the endpoint publishes
Protected Resource Metadata (RFC 9728) and the token is audience-bound to
`mcp/xtm`.

## The skill

`skills/xtm-one-platform-tools/` follows the Agent Skills specification and
carries a tool reference generated from the surface this server offered when
you downloaded the package. It also imports straight back into
{{PLATFORM_NAME}} as a skill package, if you want it attached to an agent
there.
