# MCP Registry entries

One `server.json` per XTM One MCP server published in the
[official MCP Registry](https://registry.modelcontextprotocol.io), which also
feeds the GitHub MCP registry and VS Code:

| File | Endpoint |
| --- | --- |
| `xtm-one-platform.server.json` | `/mcp/xtm` - XTM One's own tools |
| `xtm-one-agents.server.json` | `/mcp/user` - the caller's agents |
| `xtm-one-opencti.server.json` | `/mcp/opencti` |
| `xtm-one-openaev.server.json` | `/mcp/openaev` |
| `xtm-one-opencrq.server.json` | `/mcp/opencrq` |

`/mcp/global` is not published: it authenticates with an instance-level
administrator key, not with the person using the client.

Every customer runs their own instance, so each entry is a remote
`streamable-http` server whose URL is a template, `https://{xtm_one_host}/mcp/...`:
the client asks for the host at install time. The five entries declare the
same variable, so a client that merges equal inputs asks once.

`version` stays `0.0.0` here. The `MCP Registry` workflow
(`.github/workflows/mcp-registry.yml`) writes the release tag into each file
and publishes it with `mcp-publisher` on a release, once the repository
variable `MCP_REGISTRY_PUBLISH` is `true`.

`schema/server.schema.json` is the registry's `2025-12-11` schema, vendored so
CI validates the entries without network access:
`backend/tests/test_mcp_registry_entries.py` checks each entry against it and
against the routes the backend serves. See
`dev-docs/mcp-registry-publication.md`.
