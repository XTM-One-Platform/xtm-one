# Sandbox Image Hardening Guideline

Applies to every image built from `backend/sandbox/Dockerfile.sandbox.python` and
`backend/sandbox/Dockerfile.sandbox.nodejs` — these run agent-generated and
user-defined code (OpenSandbox `commands.run()` mode), and are the last line
of defense if OpenSandbox's own runtime isolation is ever weaker than expected.

If a proposed change to either Dockerfile, to `apt.sandbox.*.txt`, or to a
`requirements.sandbox.*.txt` file would violate one of the rules below,
request changes citing the specific rule number.

## Rules

1. **No compiler or build toolchain in a final image stage.** `gcc`, `g++`,
   `make`, `cmake`, `python3-dev`, `pkg-config`, and any `*-dev` header
   package belong only in the `builder` stage of `Dockerfile.sandbox.python`.
   If a new Python package needs one of these to build a wheel, add it to the
   relevant `apt.sandbox.{base,extended}.txt` (builder-only) — never to a
   `*.runtime.txt` file or to `final-full`'s `RUN apt-get install` directly.

2. **Any runtime CLI tool is a named, justified exception — never silently
   assumed build-only.** Today there are two: `tshark`/`nmap` in
   `sandbox-python-network` (`pyshark`/`python-libnmap` shell out to them at
   runtime), and `git` in `sandbox-python-rules` (`GitPython` shells out to
   `git` at import time via `git.refresh()`, not just when a repo operation
   is invoked) — see `spec.md` Clarifications under
   `specs/005-harden-sandbox-images/`. The development images of
   `Dockerfile.sandbox.dev` (`sandbox_run_command`, not in
   `sandbox-images.json`) name theirs in `apt.sandbox.dev.runtime.txt` (`git`)
   and in the `nodejs-dev` stage (a bare CPython reachable as `python`, without
   pip: the platform's workspace helper, `sandbox_workspace/remote.py`, runs
   in every command container). `python-dev` and every toolchain image
   (`toolchain-base`, #1858) name a third: the data stores of the backend's
   integration tests and of any repository's `stores = true` check (a check
   whose CI job runs a postgres service) under `/opt/stores` (a PostgreSQL 16
   server with pgvector from the `pixeltable-pgserver` wheel, Dragonfly for
   the Redis API, Silo for the S3 API). They are fetched at build time
   by `.xtm-one/stores.py` in a stage of their own, each artefact pinned by
   URL and sha256 (a mismatch fails the build), owned by root and only
   executable by `sandboxuser`; at run time the `backend-integration` check
   and the lab start them on loopback inside the check's own container, with
   no network, and the dev image smoke test asserts each one starts with
   `--network none`. They hold no data beyond the throwaway directory of one
   check. `python-dev` names a fourth: Playwright's headless Chromium shell
   and its OS libraries under `/ms-playwright` (`PLAYWRIGHT_BROWSERS_PATH`), installed at build time for the release `uv.lock` pins, owned by
   root: the lab's presentation export (PDF, PPTX) renders the deck with it,
   and the dev image smoke test prints a PDF with it under `--network none`.
   The first two were found by actually running
   the built image and importing every package, not by inspecting
   requirements files — **a package can need a runtime CLI tool
   transitively** (`sandbox-python-cti`'s `libmagic1t64` need comes from
   `pycti` eagerly importing `python-magic`, which isn't even a direct
   dependency in `requirements.sandbox.cti.txt`). Adding a package that needs
   a new runtime CLI tool or shared library means: add it to that tag's
   `apt.sandbox.*.runtime.txt` file (creating a new one if needed, and
   switching that tag's `sandbox-images.json` entry to `final_target:
   final-full` if the tool has no equivalent on `final-minimal`), and add a
   comment in that runtime file naming the package that needs it and why. Do
   not add a runtime CLI tool "just in case" — every entry in a
   `*.runtime.txt` file must trace to a specific package's documented runtime
   behavior, and **always verify by actually building and importing**, since
   transitive needs don't show up from reading requirements files alone.

3. **Base images are pinned by digest, never a floating tag.** Every `FROM`
   line in both Dockerfiles must be `image:tag@sha256:...` (or the name of an
   earlier stage), not a bare `image:tag`. When bumping a base image, resolve
   and pin the new digest in the same change — don't leave a tag unpinned
   "temporarily." A weekly grouped Dependabot pull request for
   `/backend/sandbox` moves the python, node, distroless and busybox digests
   (so keep a tag on each, e.g. `busybox:1-musl@sha256:...`). A weekly
   `Sandbox Images` run, without the layer cache, repeats every check, the
   eMudhra ones included, and opens an issue (or comments on the open one) when
   one fails, so a stale pin is reported within a week even when nobody touches the folder.

4. **Non-root execution is required.** `final-full` (Debian-slim) creates and
   switches to a fixed non-root user (`sandboxuser`, UID/GID 1000);
   `final-minimal` (distroless) already defaults to a non-root user (UID
   65532) and must not have that overridden back to root. `Dockerfile.sandbox.nodejs`
   reuses `node:24-slim`'s own built-in `node` user (also UID/GID 1000)
   rather than adding a second account at the same UID. A build-time `RUN` step
   needing root (e.g. `apt-get install`, `useradd`) must happen before the
   final `USER` switch, never after.

5. **A new package needing a fuller OS (GDAL, TensorFlow, DFIR tools, complex
   native builds) targets `final-full`, not `final-minimal`.**
   `final-minimal` (`gcr.io/distroless/cc-debian13` plus the builder's Python) has **no
   shell and no package manager** — you cannot `RUN apt-get`/`apk` or add a
   runtime CLI tool there at all. If a domain's dependencies need anything
   beyond what's already copied from the builder's venv, that domain's
   `sandbox-images.json` entry must use `"final_target": "final-full"`, not
   `"final-minimal"`. When in doubt, build both and run this document's
   verification steps (below) before deciding.

   **Console-script CLI entry points need their own check.** `import <module>`
   passing is not sufficient proof a package works on `final-minimal` — pip's
   generated console scripts (e.g. `bandit`, `celery`, `uvicorn` for
   `sandbox-python-soar`) have a hardcoded `#!/opt/venv/bin/python` shebang
   that doesn't exist on `final-minimal` at all (no `/opt/venv`, and no `env`
   binary either to fall back on `#!/usr/bin/env python3`). The Dockerfile's
   `builder` stage rewrites a standalone copy of these scripts to
   `#!/usr/local/bin/python3` (the interpreter's absolute path) and
   ships them at `/usr/local/bin` in `final-minimal` — found and fixed after
   this exact class of bug shipped silently once (no test invoked a CLI, only
   imports). If you add a package to the base/soar domain files that ships a
   console script, verify it actually **executes** (`docker run --rm
   --entrypoint <script> <image> --help`), not just that its module imports.

## Toolchain images: the named exception to rules 1 and 2

`Dockerfile.sandbox.toolchain` (targets `toolchain-python`, `toolchain-node`,
`toolchain-jvm`, `toolchain-rust`, `toolchain-dbt`, built by
`build-dev-image.sh`, not in `sandbox-images.json`) is the build environment
of a stack for any repository an agent checks out: no lock file is baked, the
checkout installs its own dependencies (uv, pip, npm, corepack's yarn and
pnpm, Maven, Cargo, dbt deps). A compiler (`build-essential`, for source
wheels, node-gyp and Cargo build scripts) and those package managers are
therefore what the image is for, and are named in
`apt.sandbox.toolchain.runtime.txt` and in the stages, as rule 2 asks. The
exception holds because of where these images run: only behind the
`AGENTIC_CODING` feature flag (Filigran-internal Copilot mode, never XTM One
customers' code interpreter or custom tools), in pods whose egress is an
allow-list of package registries or the registry proxy. Rules 3 and 4 hold
without exception: every `FROM` and every image `COPY --from=` is
digest-pinned, and every target runs as `sandboxuser` (UID 1000), owning the
toolchain directories a checkout may extend (`rust-toolchain.toml`, a
corepack-pinned yarn) so that no step needs root. Never list a toolchain image
in `sandbox-images.json` or as the `python` / `nodejs` key of a deployment.

## How to verify a change complies

Run these before opening a PR that touches a sandbox image:

- **CI smoke test**: the `sandbox-image-smoke-test` job in
  `.github/workflows/sandbox-images.yml` runs automatically on any PR touching
  `backend/sandbox/**`. It builds every
  image in `backend/sandbox/sandbox-images.json` (plus a separate
  `sandbox-nodejs-smoke-test` job) and fails if: a build-only binary is
  present, `tshark`/`nmap`/`git` is present anywhere other than
  `sandbox-python-network`/`sandbox-python-rules`, the image runs as root, or
  (via `sandbox-python-digest-check`) a `FROM` line isn't digest-pinned. This
  is the gate — a PR that regresses any of rules 1–4 above should fail it.
- **Local build + spot-check**: `./build-image.sh <image>:<tag>` builds any
  single image from `backend/sandbox/sandbox-images.json` (the same script
  `create-register-images.sh` and the CI/scan workflows call, so there is one
  place that constructs the `docker build --target ... --build-arg ...`
  invocation, not several hand-kept-in-sync copies). See
  `specs/005-harden-sandbox-images/quickstart.md` for the per-tag commands and
  the verification commands (no build tools, correct runtime-CLI-exception
  placement, non-root, pinned digests).
- **On-demand vulnerability scan**: trigger `.github/workflows/sandbox-image-scan.yml`
  manually (`workflow_dispatch`) against the image you changed. This never
  blocks a build or PR — it's a maintainer-initiated check for known CVEs, run
  whenever you want current exposure info, not a required step.
- **When bumping the `opensandbox/execd` image** (pinned in the separate
  `opensandbox-server` service, not in this repo — e.g.
  `dev-docs/opensandbox-pool-management.md`'s `opensandbox/execd:vX.Y.Z`):
  re-verify `final-minimal`'s busybox applet allowlist in
  `Dockerfile.sandbox.python`'s `busybox` build stage against the new
  `bootstrap.sh`. Extract it with
  `docker create opensandbox/execd:<version> && docker cp <cid>:/bootstrap.sh .`
  and confirm every external command it shells out to is either an `ash`
  builtin or already in the allowlist. Nothing in this repo's CI catches a
  new execd version needing a new applet — the smoke tests never exec a
  `#!/bin/sh` script, so this check is manual.

## Known caveats

- **`/opt/opensandbox/.env` write warning under the non-root user.**
  `opensandbox-server`'s `_ensure_directory` (in
  `opensandbox_server/services/docker/runtime.py`) creates `/opt/opensandbox`
  in every sandbox container via `put_archive` with a hardcoded `mode=0o755`
  and no uid/gid (root:root), regardless of anything this image's build sets
  up. With the non-root `USER` switch (rule 4), execd running as the
  sandboxed UID can no longer write `/opt/opensandbox/.env` and logs
  `warning: failed to touch EXECD_ENVS=/opt/opensandbox/.env`. This is
  non-fatal — execd warns and continues — and it's `opensandbox-server`
  behavior external to this repo, not something a change to either Dockerfile
  can fix: `_ensure_directory` resets that directory's mode/ownership on
  every sandbox creation no matter what the image ships. Worth an eyeball on
  a real sandbox after any change here that touches the runtime user, but not
  something to chase a Dockerfile fix for.

## Why this exists, and what it doesn't cover

These rules reduce the **blast radius of a container-boundary escape** — what
an attacker inherits if OpenSandbox's own isolation (namespace/cgroup
boundaries, or gVisor/Firecracker where configured) is ever weaker than
expected. They are **not** a substitute for that runtime isolation layer,
which is evaluated and chosen separately (see `sandbox.docker.toml`,
`sandbox.docker.gvisor.toml`, `sandbox.k8s.firecracker.toml`). A hardened
image with no compiler and no root user still assumes the isolation boundary
around it is the primary control — this document is defense-in-depth on top
of that, not instead of it.

Whether Filigran has an existing company-wide container-hardening standard
(from OpenCTI, OpenAEV, etc.) that should supersede this document has not
been checked from this repository — if one exists and applies here, this
document should be superseded by it rather than maintained in parallel.
