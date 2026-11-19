# Each person's own OAuth account on a shared MCP server

Issue #443. The MCP counterpart of `integration-user-credentials.md`.

## Contract

- `mcp_servers.credential_mode`: `shared` (default) or `per_user`. It only
  matters when `auth_mode = "oauth"`.
- `shared`: one token stored on the server row, used for every person who can
  use the server (today's behaviour, unchanged). On a company-managed server
  only an admin authorizes it.
- `per_user`: each person who can see the server connects their own account
  once (`GET /mcp-servers/{id}/authorize`, no admin gate); the callback stores
  the token on `mcp_server_user_credentials` (one row per server and user). A
  call made for a person carries their own token; a person who never connected,
  or a run with no acting person, sends **no** token. The token on the server
  row is never sent, never refreshed, and a stored `Authorization` header is
  dropped too: nobody is ever lent another person's account.
- `DELETE /mcp-servers/{id}/my-credential` disconnects one's own account.
- Responses carry `credential_mode` and, on a `per_user` server,
  `my_oauth_status` (the reader's own connection: `connected`, `error`,
  `disconnected`).

## Who "the acting person" is

| Path | Person |
| --- | --- |
| Chat (agent or general) | the chatting user (`load_custom_and_mcp_tools(user_id=...)`) |
| Assignment run | the run's acting identity (`_acting_identity(run.user_id, agent)`) |
| Test, Test tool, Discover tools | the caller |

## Token holders

A token holder is an `MCPServer` row or an `MCPServerUserCredential`. A
credential reads every column it does not own (OAuth client, endpoints,
`resource`, configured scopes, relay group) from its server, so the refresh
code (`services/token_refresh.py`) and the connection's auth hook
(`services/mcp_oauth_tokens.py`) work on either. A holder is named by
`mcp_holder_key`: the server id (unchanged, so a `shared` server's Redis keys
are the same as before), or `user:<credential id>`. The refresh lock, the
back-off and the forced-refresh window are keyed per holder; the sweep selects
`shared` servers and the people's credentials on `per_user` servers.

## Failure tracking

On a `per_user` server the circuit breaker and the auth escalation are keyed per
person (`MCPServerRuntime.failure_key` = `<server>:user:<user id>`, `anonymous`
without one): one person's missing account never opens the circuit for
everyone. The remediation an agent reads says the person must connect their own
account. Resource links keep naming the server (`server_key`).

## Mode change

- `shared` -> `per_user`: the server row's token is dropped.
- `per_user` -> `shared`: every person's credential is deleted (they were
  given for their own use, never to be lent). Both are audited.
- `auth_mode` back to `headers`: the row token and every person's credential
  are dropped.
- An authorization in flight for the other mode is refused at the callback.

## Frontend

- Configuration > Edit of an OAuth server: the "Connected account" picker
  (`CredentialModePicker` in `pages/mcp-server-detail/ConnectionPickers.tsx`)
  sends `credential_mode` only when it changed, and warns what saving
  discards (the shared token, or every person's own connection).
- Configuration tab of a `per_user` server: "Your account" card for every
  reader (no manage right needed): Connect your account / Connect again /
  Disconnect your account (`DELETE .../my-credential`).
- Every OAuth status badge (list, header, overview) reads
  `readerOAuthStatus()` (`lib/mcp-oauth-status.ts`): the reader's own
  `my_oauth_status` on a `per_user` server, never the server row's token.
