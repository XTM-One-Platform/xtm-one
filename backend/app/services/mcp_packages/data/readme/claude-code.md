# {{PLATFORM_NAME}} platform tools — Claude Code plugin

Gives Claude Code the {{PLATFORM_NAME}} platform tools: agent traces, agents,
assignments, runs, knowledge bases, prompts, skills, custom tools,
integrations and variables. It ships the matching skill, so the model knows
how to use them rather than guessing.

{{INSTALL_AUTH}}

## What you get

- `inspect_agent_traces` / `get_trace_payload` — read what an agent actually
  did, across agents or one at a time.
- The full management surface, at your own permissions: what you can see and
  change here is what you can see and change in the {{PLATFORM_NAME}} UI.
- The `xtm-one-platform-tools` skill, including a reference generated from the
  tool surface this server offered when you downloaded the package.

## Verify

```bash
claude
> /mcp
```

The `xtm-one-platform` server should be listed as connected. If it is not,
check that an administrator has enabled the server in
**Settings > Integrations > MCP Endpoint** — a disabled server answers `403`.
