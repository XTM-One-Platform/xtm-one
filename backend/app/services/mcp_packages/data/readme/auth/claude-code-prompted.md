## Install

Unzip this archive, then, from the directory that contains it:

```bash
claude plugin marketplace add ./xtm-one-platform
claude plugin install xtm-one-platform@xtm-one
```

The install asks for your API key and stores it in Claude Code's secure
storage — it is never written into this package or into `settings.json`.

Create the key in **My Profile > API Keys** on {{PLATFORM_NAME}}. Use a
dedicated key for this client so you can revoke it on its own.

### Why a marketplace rather than `--plugin-dir`

`claude --plugin-dir ./xtm-one-platform` loads the plugin but does **not**
prompt for `userConfig`, so the `Authorization` header would reach the server
with an unresolved `${user_config.api_token}` and every call would 401. The
single-plugin marketplace in this package exists to make the prompting install
path available from a local directory.

(A package downloaded *with* its key embedded has no placeholder to resolve,
and `--plugin-dir` works for it.)
