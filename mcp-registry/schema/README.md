# Vendored MCP Registry schema

`server.schema.json` is the `server.json` schema of the official MCP Registry,
version `2025-12-11`, copied unchanged from
<https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json>
(generated from `docs/reference/api/openapi.yaml` in
[modelcontextprotocol/registry](https://github.com/modelcontextprotocol/registry),
licensed MIT / Apache-2.0).

SHA-256: `3fba09590c99f61735d234822279f4223fab9e300c0a81e81c91ab62a4114de0`.

It is pinned on purpose: an entry's `$schema` names this version, and
`backend/tests/test_mcp_registry_entries.py` fails when the two differ. To move
to a newer schema, download it, update the `$schema` of every entry, the
checksum here and `SCHEMA_VERSION` in that test, in the same change:

```bash
curl -sSfL -o mcp-registry/schema/server.schema.json \
  https://static.modelcontextprotocol.io/schemas/<version>/server.schema.json
```
