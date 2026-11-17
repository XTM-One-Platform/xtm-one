# Phase 1 Data Model: Harden OpenSandbox Docker Images

This feature has no application data model (no database tables, no API payloads). The "entities" below are build-time/configuration concepts realized as files and CI artifacts, carried over from the spec's Key Entities section and made concrete against this repository's actual files.

## Sandbox image variant

Represents one buildable, publishable Docker image OpenSandbox instantiates.

| Field | Value / Source |
|---|---|
| Name | `sandbox-python:0.2` (base), `sandbox-python-{cti,data,malware,network,rules,soar}:0.2` (domain images), `sandbox-nodejs:0.2` — 8 tags total |
| Defined by | `backend/sandbox/Dockerfile.sandbox.python` (all 7 Python tags, differing in `--build-arg APT_FILE=... REQ_FILE=... RUNTIME_APT_FILE=...` and, per tag, `--target final-minimal\|final-full` — see `create-register-images.sh`/`sandbox-images.json` for the exact per-tag values), `backend/sandbox/Dockerfile.sandbox.nodejs` |
| Published by | `.github/workflows/build-push-sandbox.yml` → `xtmone/sandbox-python`, `xtmone/sandbox-nodejs` on Docker Hub **only**; the 6 domain images are built by `create-register-images.sh` for local k3s dev and are not CI-published (unchanged by this feature) |
| Consumed by | `Sandbox.create(image=...)`; production defaults both `OPENSANDBOX_IMAGES` keys (`python`, `nodejs`) to `sandbox-python:0.2`/`sandbox-nodejs:0.2` (`_OPENSANDBOX_DEFAULT_IMAGES` in `backend/app/core/config.py`); an operator can override a key to point at a domain image if they build and host it themselves |
| Hardening attributes (this feature) | base image digest, final-stage apt package list (per-tag, since `APT_FILE` differs), non-root `USER`, absence of build-only tooling |

State/lifecycle: built → (optionally) scanned on demand → published (tagged in Docker Hub) → pulled by an OpenSandbox pool at container-create time. No state persisted by this feature beyond the image layers themselves and Docker Hub's tag metadata.

## Build-only dependency (apt package classification)

A per-package classification, not a stored record — expressed directly as which Dockerfile stage installs a given apt package.

| Field | Meaning |
|---|---|
| Package name | e.g. `gcc`, `libmagic1`, `tshark` |
| Classification | `builder-only` (dropped from final stage) / `runtime-shared-lib` (kept, no CLI surface) / `runtime-cli-exception` (kept, documented — currently only `tshark`, `nmap` in `sandbox-python-network`) |
| Owning image variant(s) | which of the 8 tags (7 `sandbox-python*` + `sandbox-nodejs`) installs it |
| Rationale | one-line comment in the Dockerfile / `apt.sandbox.*.txt`, per Constitution Principle IV |

See `research.md` §2 for the concrete classification of every current `apt.sandbox.extended.txt` entry.

## Hardening guideline (document)

| Field | Value |
|---|---|
| Location | `dev-docs/sandbox-image-hardening.md` (new) |
| Owner | CODEOWNERS `/dev-docs/` rule (`@fred-lr @davidloiret @antoinegaston`) |
| Referenced from | `backend/sandbox/SANDBOX.md` (added pointer) |
| Content shape | Rules a reviewer can cite: (1) no compiler/build toolchain in a final image stage, (2) any new runtime-CLI exception (like `tshark`/`nmap`) must be named and justified in the Dockerfile, (3) base images pinned by digest, (4) non-root `USER` required, (5) how to run the on-demand scan and the CI smoke test locally |

## Vulnerability scan result

Not persisted in this repository — it is the transient output of a manually-triggered CI run.

| Field | Source |
|---|---|
| Trigger | `workflow_dispatch` on `sandbox-image-scan.yml`, inputs: image name, tag |
| Producer | `aquasecurity/trivy-action` job step |
| Output | GitHub Actions step summary / uploaded SARIF artifact for the run that triggered it; not written back into the repository or into a tracked file |
| Gating behavior | None — always exit 0 regardless of findings (spec FR-008) |
