# Editions: which source files are Enterprise Edition

`LICENSE` describes two editions: the XTM One Community Edition (CE), under the Apache License 2.0, and the XTM One Enterprise Edition (EE), under the XTM One Enterprise Edition License. A source file tells which one it belongs to by its header: **an EE file starts with the EE notice, a file without it is CE**. This is the OpenCTI and OpenAEV model (#5493).

This is about the licence of the source, not about runtime: what a deployment may run is decided by the edition gate (`is_agentic_ai_licensed()`, invariant 17), which this file does not touch.

## The rule

**Everything agentic is EE, Knowledge Bases included. The modules of the Platform MCP Servers (`/mcp/opencti`, `/mcp/openaev`, `/mcp/opencrq`) stay CE.** They still reach EE files at run time (the integration tools, the approval and consent helpers, API key quotas): CE code may import EE code (below), so "CE" here is about the licence of those modules, not about what they load. Everything that is not agentic is CE: authentication and SSO, users and groups, settings, platform registration, audit logs, files, CTEM metrics, integrations, models and migrations, `app/llm`, `app/enterprise` and the edition gate itself.

A few building blocks inside agentic areas stay CE because CE code is built on them (`tools/base.py`, the tool contract the integration providers and the Platform MCP Servers use).

## Where it lives

| What | Where |
|---|---|
| The EE list | `licensing/editions.yml`: `ee` globs, `except` (CE files inside an EE glob), `third_party` (files of other owners inside an EE glob), `no_header` (tests), `mixed` and `ce` (the directories, below) |
| The CE marker | `SPDX-License-Identifier: Apache-2.0`, the first line of each CE source file of a mixed directory: the decision, in the file (`license_headers.py --mark-ce`) |
| The notice | The "XTM One Enterprise Edition License" section of `LICENSE`, read at run time, never copied |
| The one module | `backend/scripts/license_headers.py` (`--check`, `--fix`) |
| The tripwire | `license_headers.py --check`, run by the `Editions` workflow on every pull request and merge group (the rules themselves: `backend/tests/test_license_headers.py`) |
| Before writing a file | `make edition P=<path>` (`--which`) |
| Tagging the tree, checking a commit | `make license` (`--fix`), `make setup-hooks` and `.githooks/pre-commit` (`--staged`) |
| The review | `.github/workflows/editions.yml` (every file is classified), `.github/instructions/licensing.instructions.md` (the review agent) |

The header is a `#` block in Python, shell and PowerShell and a `/* */` block in TypeScript, JavaScript, Rust and CSS, as in OpenCTI. It sits before the module docstring and the imports, after a shebang, a Python encoding cookie or PowerShell `#Requires` lines. Every source file takes it: `LICENSE` reads a file without a header as Community Edition, so the code of the EE apps (Rust included) carries it too. A `<dir>/**` glob with no `except` under it also puts a `copyright.md` in that directory, for the files a comment cannot mark (manifests, data, assets).

## Third-party files and package manifests

A file copied or adapted from another owner (vendored schemas, a price table, a skill pack, customer and partner logos) is neither EE nor CE: it stays under its owner's license or terms. It is listed under `third_party`, never gets the header, and the `copyright.md` of its directory names it after the notice. Its license text and origin go in a `NOTICE` file next to it (`seed_data/NOTICE-cti-pack.md`, `seed_data/NOTICE-model-prices.md`, `agentschema/schemas/NOTICE.md`).

A package manifest under an EE glob declares the EE license as `LicenseRef-XTM-One-Enterprise-Edition`, the SPDX form of a license that is not on the SPDX list: the `license` of a `Cargo.toml` or a `package.json` and the `org.opencontainers.image.licenses` label of a `Dockerfile`. `--fix` rewrites a declaration that says otherwise; a manifest that declares no license is left as it is.

A package that ships both editions declares both, `Apache-2.0 AND LicenseRef-XTM-One-Enterprise-Edition` (`MIXED_LICENSE`): `backend/pyproject.toml`, `frontend/package.json` and the labels of the main images (`Dockerfile`, `Dockerfile.worker`). Declaring `Apache-2.0` alone there would read as Apache for the EE files they hold. Only a CE-only package (the docs) declares `Apache-2.0`. `test_the_package_metadata_declares_what_each_package_holds` pins them.

## Directories: EE, CE or mixed

Nothing becomes CE without a decision. Each directory under `backend/app`, `frontend/src` and `apps` is decided once:

| Kind | Where | A new file there | A new child directory |
|---|---|---|---|
| Wholly EE | a `<dir>/**` glob in `ee` | EE (header, `copyright.md`) | EE |
| Wholly CE | `ce` | CE | CE |
| Mixed | `mixed` (the three trees first) | classified one by one: an `ee` glob, or the CE marker on its first line | classified: `<dir>/**` in `ee`, `ce` or `mixed` |

A directory under a wholly EE or wholly CE one follows it; only the children of a mixed one are asked about. A directory that holds only tests follows no edition. `app/integrations` and `app/llm` are CE for now, as the rule above says, but product has still to confirm it (`# to confirm (product)`): until then they are `mixed`, so each of their files carries the CE marker and a new one is a decision. `app/integrations/providers` is `mixed` too: each connector directory is `ce` on its own line, `providers/openaev` is `mixed` (it holds the autonomous attack-path agent), and a connector module added directly in `providers/` is a decision like any file of a mixed directory.

Only source files (`.py`, `.ts`, `.tsx`, `.js`, `.mjs`, `.rs`, `.css`, `.sh`, `.ps1`) are asked about in a mixed directory. A data file there (JSON, YAML, Markdown, an image) stays CE by default, even when an EE module reads it: neither a header nor, outside a wholly EE directory, a `copyright.md` can mark it. Data that belongs to an EE feature goes into a wholly EE directory (`<dir>/**`), or is listed `third_party` when another owner's.

## Adding or moving a file

The edition is settled **before** the file is written, because the two directions are not equal: a file can move from EE to CE later, never from CE to EE once a version holds it under Apache.

1. Ask the tool, before creating the file or the directory (the path need not exist): `make edition P="backend/app/services/my_thing.py"`, or `P="backend/app/services/my_package/"` for a directory (a path that ends with `/`, is a directory on disk, or names no file and has no suffix). It prints `EE`, `EE (unmarked)` (a non-source file that neither a header nor a `copyright.md` can mark), `CE`, `to classify`, `CE (default)`, `third-party` or `unmarked` (tests), and what decided it. A directory is `EE`, `CE`, `mixed` or `to classify`. `to classify` is a source file of a mixed directory, or a new directory in one, or any file of such a directory: the build fails until it is classified, and `make edition` exits with an error so a hook or an agent can tell.
2. Classify it in the same change, by the rule above: agentic is EE. A file: a glob in `ee` (prefer a name prefix to a single file), or the CE marker as its first line, after a shebang (`make mark-ce P=<path>` writes `# SPDX-License-Identifier: Apache-2.0`, `// ...` in TypeScript, JavaScript and Rust, `/* ... */` in CSS). The decision sits in the file, not in a shared list, so pull requests that add files side by side never conflict on it; `license_headers.py --fix` never writes the marker. A new directory: `<dir>/**` in `ee`, its path in `ce`, or in `mixed` when it holds both. When in doubt, treat it as EE and ask product.
3. **A CE file moved, renamed or split keeps its edition.** The code was published under Apache: the new file is CE too (it keeps the CE marker), even in an EE directory, where it goes to `except`. Moving CE code into an EE file does not make it EE.
4. Never write the header by hand: `make license` (`--fix`) adds, updates or removes it and the `copyright.md` files. `make setup-hooks` (once per clone) enables `.githooks/pre-commit`, which runs `license_headers.py --staged`: it reads what the commit holds (the index, never the working tree: the files, `licensing/` and `LICENSE` alike), writes nothing, and runs the same file check as CI (`file_problems`) on the files the commit holds and the directories it removes a file from, every file when the commit changes `LICENSE` or `licensing/editions.yml` (and then every list problem, not only the ones it touches). It reads regular files only, never a symlink or a submodule, and a list or `LICENSE` it cannot read stops the commit with what to fix, not a traceback. It stops the commit on a missing header, a CE file that still carries one (an EE file moved out of EE: classify its new path, `make license` would strip the header), a `copyright.md` missing or outside an EE directory, a file that is not UTF-8, or a list problem the commit touches; the message says what to do, in order. It runs in its own small environment (`uv run --no-project` with the PyYAML of `uv.lock`), so a commit never syncs the backend; without uv, or when uv cannot set that environment up, it is skipped with a warning. `core.hooksPath` is shared by every worktree of the clone; `make setup-hooks` leaves an existing value alone. `git commit --no-verify` skips the hook; CI does not.

The tripwire, `license_headers.py --check` in the `Editions` workflow, fails on a glob that matches no file (`no_header` included: it wins over `ee`), a new directory or a new source file of a mixed directory that nobody classified, a `ce` or `mixed` entry that is no directory, is not in a mixed one, is classified twice or (for `ce`) holds an EE file, and an EE file that carries the CE marker (a decision to make: remove the marker, or narrow the glob). It also fails on an EE file without the exact header, a CE file that carries it, a `copyright.md` that is missing or outdated and an EE manifest that declares another license.

## Review

The tripwire checks that a decision exists, not that it is right: an agentic file marked CE passes it, and so does a copy of a CE file, which carries the marker of the file it came from. No workflow holds a pull request that makes a file CE (the code owner approval gate was removed by CEO decision, 13 Nov 2026); the review is where that decision is checked. `license_headers.py --new-ce BASE` lists every file under `backend/app`, `frontend/src` and `apps` that reads as CE at the head and did not on its base, for a reviewer who wants the list:

- a new file the base list did not already make CE: one with the CE marker (a copy included), one in a new `ce` directory, one under a new `except` entry. A new file in a directory that was already `ce` is not listed: that directory was decided;
- an existing file that an `ee` glob no longer covers, that a new `except`, `third_party` or `no_header` entry takes, or that gains the CE marker.

A renamed file keeps the decision of its old path. The review agent (GitHub Copilot code review) follows `.github/instructions/licensing.instructions.md`: an agentic file or directory classified CE, CE code moved into an EE file, a `ce` directory that agentic code is about to land in. The `Editions` workflow checks the classification and the header of every file in seconds (`--check`), outside the backend suite.

To change the copyright year, edit `LICENSE` and run `--fix`: the result is one generated commit.

## What the edition does not constrain

CE code may import EE code. OpenCTI does the same: its CE authentication and settings call EE files, and its EE managers start on every installation and check the edition at run time. The licence comes from the header, not from the import graph, so no import rule is enforced.

## Differences from OpenCTI

- `app/enterprise` (licence certificates, the edition gate) is CE: a CE installation must be able to tell that it is CE. In OpenCTI, `enterprise-edition/ee.ts` and `modules/settings/licensing.ts` are EE.
- `apps/` carries the header on every source file (TypeScript, Rust, shell, PowerShell, CSS) and one `copyright.md` per app for its manifests and assets.
- Tests never carry the header.
