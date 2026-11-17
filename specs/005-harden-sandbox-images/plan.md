# Implementation Plan: Harden OpenSandbox Docker Images

**Branch**: `005-harden-sandbox-images` | **Date**: 2026-08-10 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/005-harden-sandbox-images/spec.md`

## Summary

Harden the OpenSandbox container images that execute agent-generated and user-defined code — `sandbox-python:0.2` (base), six domain images (`sandbox-python-{cti,data,malware,network,rules,soar}:0.2`), and `sandbox-nodejs` — so that a container-boundary escape inherits the smallest possible toolkit and privilege. Technical approach: convert `Dockerfile.sandbox.python` to a multi-stage build that compiles/installs dependencies in a `builder` stage and copies only the resulting virtualenv plus required runtime shared libraries into a minimal final stage (P1); evaluate and adopt a hardened/minimal, non-root, digest-pinned base for both Dockerfiles (P2); add an on-demand (manually-triggered, non-blocking) vulnerability scan of built images (P3); and publish a hardening guideline under `dev-docs/` (P4).

## Technical Context

**Language/Version**: Dockerfile (BuildKit multi-stage), Python 3.14 (image runtime), GitHub Actions YAML (CI)

**Primary Dependencies**: Docker Buildx (`docker/build-push-action`, already used by `.github/workflows/build-push.yml`); Trivy (`aquasecurity/trivy-action`) for on-demand vulnerability scanning (see research.md); no new Python/JS runtime dependencies

**Storage**: N/A

**Testing**: Docker image build + smoke-test, wired into CI as a new blocking job distinct from the non-blocking on-demand vulnerability scan. Covers all 7 `sandbox-python*` tags (`sandbox-python` base plus the `-cti`/`-data`/`-malware`/`-network`/`-rules`/`-soar` domain images built from the same `Dockerfile.sandbox.python` with different `APT_FILE`/`REQ_FILE` build-args, per `create-register-images.sh`) plus `sandbox-nodejs`, running `python -c "import <pkg>"` / `node -e` checks against each image's actual requirement file.

**Target Platform**: Linux containers, run via the existing OpenSandbox backends (`sandbox.docker.toml`, `sandbox.docker.gvisor.toml`, `sandbox.k8s.firecracker.toml`) — this feature changes only what is baked into the image, not which backend runs it

**Project Type**: Infrastructure/build tooling change within the existing single-repo layout (no new frontend/backend split)

**Performance Goals**: N/A (not a latency-sensitive path); build time must stay within the existing 30-minute CI job timeout (`build-push.yml`)

**Constraints**: Every package currently installable via `requirements.sandbox.base.txt` and each domain requirement file (CTI, network, malware, SOAR, data, rules) MUST remain installable/importable or have its removal explicitly documented (spec FR-003); image tags and `Sandbox.create(image=...)` usage MUST remain stable (spec FR-010); `tshark`/`nmap` MUST remain runtime-present in `sandbox-python-network` only (spec Clarifications)

**Scale/Scope**: 8 image tags built from 2 Dockerfiles (`sandbox-python` base + 6 domain images `-cti`/`-data`/`-malware`/`-network`/`-rules`/`-soar`, all sharing `Dockerfile.sandbox.python`; plus `sandbox-nodejs`); only `sandbox-python`/`sandbox-nodejs` are CI-published to Docker Hub today — the 6 domain images are local-dev-only (`create-register-images.sh`) and stay that way (publishing them is out of scope, per spec Clarifications); ~200 packages across 5 domain requirement files; 1 existing CI workflow (`build-push-sandbox.yml`, unchanged) plus 1 new CI smoke-test job and 1 new on-demand scan workflow; 1 new guideline doc

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- [x] **Fail-Closed by Default** (Principle I): Not directly applicable — this feature has no new runtime authorization/quota/signature check that could silently pass on backend failure. It is itself a defense-in-depth measure consistent with the constitution's Security Requirements ("module/import blocklists are defense-in-depth, not a complete security boundary on their own"); no gate violation.
- [x] **Concurrency-Safe, Multi-Replica by Design** (Principle II): Not applicable — the on-demand vulnerability scan is a manually-triggered `workflow_dispatch` run by a human, not a scheduled job or shared-counter mutation executed by concurrent replicas.
- [x] **CI-Enforced Quality Gates** (Principle III): Applicable and satisfied by design — a new CI smoke-test job builds every image variant (base + 6 domain images + nodejs) and verifies each one's own requirement-file packages still install and import, gating merges the same way `ci.yml`'s lint/typecheck/integration jobs do today (the smoke-test job itself lives in the path-filtered `sandbox-images.yml`, split out of `ci.yml` during review). (The vulnerability scan itself is deliberately non-blocking per spec Clarifications — that exception is scoped to the scan only, not to this new build/import smoke test.)
- [x] **Rationale-Documented Complexity** (Principle IV): Applicable — the multi-stage Dockerfile's package classification (build-only vs. runtime, including the `tshark`/`nmap` exception) and the base-image evaluation outcome MUST carry "why" comments in the Dockerfiles themselves, not just in this plan.
- [x] **Backward-Compatible, Auditable Evolution** (Principle V): Not applicable — no schema or persisted state change; container image content is not covered by the Alembic-migration gate.

No unjustified violations. Complexity Tracking table below is not needed.

## Project Structure

### Documentation (this feature)

```text
specs/005-harden-sandbox-images/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md         # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

No `contracts/` directory: this feature changes Dockerfile/CI internals, not a public API, CLI schema, or other externally-consumed interface. The one existing external contract — `Sandbox.create(image="sandbox-python:0.2"/"sandbox-nodejs:0.2"/..., ...)`, defaulted via `_OPENSANDBOX_DEFAULT_IMAGES` — is explicitly required to stay unchanged (spec FR-010) rather than being redefined by this feature.

### Source Code (repository root)

```text
backend/sandbox/
├── Dockerfile.sandbox.python      # Rewritten as multi-stage (builder + final); base-image line pinned by digest
├── Dockerfile.sandbox.nodejs      # Base-image line pinned by digest; non-root USER added
├── apt.sandbox.base.txt           # Unchanged contents; consumed by the builder stage only (final-minimal has no apt at all — see research.md §3a)
├── apt.sandbox.extended.txt       # Unchanged, builder-stage only. Final-stage runtime subsets come from the runtime files below, per research.md §2
├── apt.sandbox.base.runtime.txt      # Documented fallback only (research.md §3a) — not consumed by any tag today; sandbox-python/-soar stay on final-minimal, which has no apt to install it with
├── apt.sandbox.extended.runtime.txt  # final-stage subset for sandbox-python-data (shared-lib set from research.md §2)
├── apt.sandbox.malware.runtime.txt   # apt.sandbox.extended.runtime.txt's contents + DFIR libs (pytsk3/dfvfs/plaso) — sandbox-python-malware only, added post-review
├── apt.sandbox.network.runtime.txt   # apt.sandbox.extended.runtime.txt's contents + tshark, nmap, libkrb5 — sandbox-python-network only
├── apt.sandbox.cti.runtime.txt       # New, found during implementation (research.md §3a): libmagic1t64 — pycti transitively imports python-magic
├── apt.sandbox.rules.runtime.txt     # New, found during implementation (research.md §3a): git — GitPython shells out to it at import time
├── requirements.sandbox.*.txt     # Unchanged
└── SANDBOX.md                     # Updated to reference the new hardening guideline

dev-docs/
└── sandbox-image-hardening.md     # New: User Story 4's hardening guideline

.github/workflows/
├── build-push-sandbox.yml         # Existing manual publish workflow; unchanged trigger shape
├── sandbox-image-scan.yml         # New: manually-triggered (workflow_dispatch), non-blocking vulnerability scan (User Story 3)
└── sandbox-images.yml             # New: path-filtered on backend/sandbox/**, build+smoke-test job covering all 7 sandbox-python* tags + sandbox-nodejs (User Story 1/2 validation); split out of ci.yml during review
```

**Structure Decision**: Single-project layout, no new top-level directories. All Dockerfile/dependency-file changes stay inside the existing `backend/sandbox/` directory; the new guideline follows the existing `dev-docs/` convention (already CODEOWNERS-covered); the new scan workflow follows the existing `.github/workflows/` reusable-workflow pattern already used by `build-push-sandbox.yml` → `build-push.yml`.

## Complexity Tracking

*No Constitution Check violations — table not needed.*
