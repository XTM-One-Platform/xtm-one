## Install

Unzip this archive, then, from the directory that contains it:

```bash
claude --plugin-dir ./xtm-one-platform
```

That is the whole setup. The endpoint URL and an API key are already in the
package, so nothing is asked of you and nothing is left to configure.

To keep the plugin across sessions rather than loading it per run:

```bash
claude plugin marketplace add ./xtm-one-platform
claude plugin install xtm-one-platform@xtm-one
```

## This file is a credential

The package was generated with a **new API key** created for it, named
`{{KEY_NAME}}`, written into `.mcp.json`. Treat the archive like the key it
contains:

- do not commit it, share it, or attach it to a ticket;
- delete it once installed — Claude Code has stored what it needs;
- revoke the key in **My Profile > API Keys** if the file goes anywhere it
  should not have. It is a key of its own, so revoking it breaks nothing else.

Prefer to be asked for the key at install time? Download the package again
without the key, from **My Profile > MCP Endpoint**.
