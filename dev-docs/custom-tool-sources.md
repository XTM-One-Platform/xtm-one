# Custom tool sources and declared tests

Two features that work together: a custom tool can declare **tests** (data
cases and test code, with a **dry run**), and custom tools can be **synced from
a GitHub or GitLab repository**, where a new version is activated only when its
tests pass. User-facing description: `docs/docs/usage/resources/tools.md`.

## Declared tests (`services/custom_tool_tests.py`)

`CustomTool.test_cases` (JSON list) and `CustomTool.test_code` (text), both
optional, validated by `validate_test_cases` / `validate_test_code` on every
write (REST create / update / import, the sync).

- **Data case**: `name`, `inputs`, `dry_run`, `expect` (`equals`, `contains`,
  `not_contains`, `regex`, `json_subset`; every given check must hold),
  `expect_error` / `error_contains`. One `execute_tool_auto` run per case.
- **Test code**: run through `execute_tool_auto` too, as a harness built around
  the tool's code (`build_python_harness`, `build_js_harness`), so it gets the
  same sandbox, blocklist and egress policy as the tool. Python collects
  `test_*` functions (async ones too) and `unittest.TestCase` classes;
  JavaScript collects `test` / `it` calls and offers `assert`
  (`node:assert/strict`). Both expose `run_tool` / `runTool` (returns what the
  tool printed, raises `ToolError`) and print one `__XTM_TEST_RESULTS__` line
  the backend parses. Standard library only: the sandbox images carry no
  pytest, and a `node:test` import would run its tests after the harness exits.
- `run_tool_tests` returns a report (`status`: `passed` / `failed` / `error` /
  `no_tests`, counts, per-result output clipped to 4000 characters).
  `POST /custom-tools/{id}/run-tests` keeps a **full** run in
  `last_test_status` / `last_test_run_at` / `last_test_results`; a run of
  selected cases is not the tool's status.

### The dry run is a contract

`execute_tool_auto` injects `dry_run` (`$dry_run` in JavaScript) into every
run, `False` unless the caller sets it, so a tool can test it unconditionally;
a tool whose own schema declares `dry_run` keeps the value it was given. A
dry-run case, the `Dry run` switch of `Test Tool` and `dry_run_only` (which also
forces it in the harness's `run_tool`) set it to `True`. The platform cannot
enforce it: the sandbox leaves the network open because a tool reads through
it too. The UI says so wherever the dry run is offered.

## Repository sources (`services/custom_tool_sources.py`)

`CustomToolSource`: `provider` (`github` / `gitlab`), `repository`
(`owner/repo`, `group/.../project`; a pasted URL is normalized and its host
dropped), `ref` (default branch when unset), `path`, `integration_id`,
`sync_interval` (`manual`, `6h`, `12h`, `daily`, `weekly`), `detached_paths`,
the last sync's commit, status, error and report.

### Reading

- Credentials: only the bound integration, never auto-discovered (the contract
  of skill sources): connected, of the source's provider, not per-user (the
  sync is unattended), and usable by the person who binds it
  (`require_oauth_credentials_usable`). OAuth token first, then the API key.
  A GitLab integration's `base_url` sets the instance; without an integration
  the source reads github.com / gitlab.com anonymously. Requests follow the
  integration's relay route (`egress_route`).
- GitHub: commit of the ref, then the tree of the sub-directory (walked one
  level at a time from the commit, then read recursively, so a monorepo syncs
  as long as the directory holding the tools fits in one read), then the files
  through the contents API (authenticated) or `raw.githubusercontent.com`
  (anonymous: the API's anonymous budget is 60 requests an hour). GitLab:
  commit, paginated tree, raw files. Files over 512 KB, more than 200 tools,
  or a truncated GitHub tree are refused.
- A configured sub-directory with no file in it is an error, never "every tool
  was removed".

### Manifest

`tool.yaml` (or `tool.yml`), `extra="forbid"`: `name`, `description`,
`language` (else inferred from the entry's extension), `entry` (default
`main.py`, `main.js`, `index.js`), `input_schema` (an object schema),
`timeout` (1 to 3600), `tags`, `icon_url`, `hide_from_chat` (applied at
creation only), `tests` (data cases), `test_file` (default `test_main.py`,
`main.test.js`, `test.js`). Paths stay inside the tool's directory.

### Sync

A tool's identity is its directory (`CustomTool.source_path`), so a rename
keeps the agents bound to it. `content_hash` (sha256 of the manifest, entry
and test file) skips an unchanged tool. A new or changed tool runs its tests
with the source owner's variables; it is applied only when the status is
`passed` or `no_tests` (`ACTIVATING_STATUSES`). A rejected version leaves the
active one and its test status untouched. A tool gone from the repository is
disabled and marked (`CustomTool.source_removed_at`); when its directory comes
back, the sync enables it again (`restored` in the report), unchanged or once
its new version passes, and never a tool disabled by hand. A tool that cannot
be read is reported and left as it is; a detached directory is skipped.

One sync of a source at a time: `run_sync` takes the source's row lock
(`FOR UPDATE SKIP LOCKED`) until its commit, so two syncs cannot both create
a new tool. While one runs, `POST /{id}/sync` answers 409 and the scheduler
skips the source (`SourceSyncBusyError`). A sync that fails to read the repository rolls back
everything it wrote and backs off exponentially. `run_sync` writes one audit
entry per sync that changed something (`log_system_audit` from the scheduler).

The autonomous loop runs due sources (`process_scheduled_syncs`), with no AI
gate (a tool is a script) but the licence and the `disable_custom_tools`
switch.

### Lock and detach

`helpers.source_managed_custom_tool_error` is the one refusal, used by the REST
routes and the agent builder: a synced tool's content fields
(`SOURCE_MANAGED_CUSTOM_TOOL_FIELDS`), its company-managed flag and its owner
cannot change, and it cannot be deleted, while sharing, `enabled` and
`hide_from_chat` stay editable. The REST update refuses only a value that
**changes** (the configuration form sends the fields back unchanged with a
sharing edit). `POST /custom-tools/{id}/detach` (the source's owner or an
admin) clears the link and adds the directory to `detached_paths`. Deleting a
source detaches its tools unless `delete_tools=true`. `custom_tools.source_id`
has no foreign key (it would lock `custom_tools` at migration time): a tool
whose source is gone is an ordinary tool.
