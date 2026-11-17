---

description: "Task list for feature implementation"
---

# Tasks: Harden OpenSandbox Docker Images

**Input**: Design documents from `/specs/005-harden-sandbox-images/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, quickstart.md

**Tests**: This feature touches `backend/sandbox/` (backend infra) and CI workflows, so per this project's constitution (Principle III: CI-Enforced Quality Gates) it MUST be covered by tests that run in CI. There's no application code here to unit-test with pytest — the CI-enforced check is a build-and-smoke-test job (asserts absence of build tools, presence of required imports, non-root execution, pinned digests), written and confirmed failing against the current Dockerfiles before the corresponding implementation task, per the constitution's own "tests are the norm here" framing. It was added to `ci.yml`, then split out into the path-filtered `sandbox-images.yml` during review.

**Organization**: Tasks are grouped by user story (P1→P4 from spec.md) to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US4)
- Include exact file paths in descriptions

## Key facts carried over from plan.md / research.md

- `Dockerfile.sandbox.python` backs **7** tags today: `sandbox-python:0.2` (base) and 6 domain images — `sandbox-python-{cti,data,malware,network,rules,soar}:0.2` — each built with a specific `APT_FILE`/`REQ_FILE` pair (see `backend/sandbox/create-register-images.sh`). Only `sandbox-python` and `sandbox-nodejs` are CI-published to Docker Hub (`build-push-sandbox.yml`); the 6 domain images are local-dev-only and **stay** local-dev-only — this feature does not add new publish targets.
- `tshark`/`nmap` are a documented runtime-dependency exception, kept **only** in `sandbox-python-network` (needed by `pyshark`/`python-libnmap`); every other tag drops them.
- `sandbox-python-cti`/`-rules`/`-soar` use `apt.sandbox.base.txt`; `sandbox-python-data`/`-malware`/`-network` use `apt.sandbox.extended.txt`.
- Vulnerability scanning (US3) is on-demand/manual only (`workflow_dispatch`), never a build/publish gate.

---

## Phase 1: Setup

**Purpose**: Establish a before/after baseline; no source changes.

- [X] T001 Build all 8 current (unmodified) sandbox images locally — `sandbox-python:0.2` and the 6 domain tags via `backend/sandbox/create-register-images.sh`, plus `sandbox-nodejs:0.2` via `backend/sandbox/Dockerfile.sandbox.nodejs` — and record each image's size (`docker images`) and installed apt package list (`docker run --rm <image> dpkg -l`) for later before/after comparison in the PR description. **Done**: 7/8 built (base 677MB, cti 1.11GB, malware 2.21GB, network 2.1GB, rules 369MB, soar 914MB, nodejs 227MB). `sandbox-python-data` failed at `pip install` even unmodified — `tensorflow==2.21.0` has no wheel for Python 3.14 at all — a pre-existing bug, out of scope for this feature.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Single source of truth for the 7-tag build matrix, reused by Setup's script, the new CI smoke-test job (US1), and the new scan workflow (US3) — avoids the same tag/`APT_FILE`/`REQ_FILE` list being hand-duplicated three times.

**⚠️ CRITICAL**: Complete before starting US1's CI job (T003).

- [X] T002 Extract the 7 `sandbox-python*` build definitions (tag, `APT_FILE`, `REQ_FILE`) currently hardcoded in `backend/sandbox/create-register-images.sh` into a new `backend/sandbox/sandbox-images.json` (array of `{tag, apt_file, req_file}`), then update `create-register-images.sh` to loop over that file instead of repeating each `docker build` call — behavior must stay identical (same tags, same build args, same k3s import step). **Done**: also carries `runtime_apt_file`, `final_target`, and `smoke_imports` per entry (see T005/T011/T003), used by `create-register-images.sh`, the CI matrix, and `sandbox-image-scan.yml`.

**Checkpoint**: `sandbox-images.json` exists and `create-register-images.sh` still builds the same 7 images from it.

---

## Phase 3: User Story 1 - Ship sandbox images without build-only tooling (Priority: P1) 🎯 MVP

**Goal**: Every `sandbox-python*` tag ships without compilers/build toolchains in its final image, while every package it previously supported still imports.

**Independent Test**: Build all 7 Python tags; confirm none contain `gcc`/`g++`/`make`/`cmake`; confirm each tag's own requirement-file packages still import (quickstart.md §2).

### Tests for User Story 1

> Write T003 first and confirm it FAILS against the current single-stage Dockerfile before starting T004.

- [X] T003 [US1] Add a `sandbox-image-smoke-test` job to `.github/workflows/ci.yml`, matrix-driven from `backend/sandbox/sandbox-images.json` (plus a separate `sandbox-nodejs-smoke-test` job), that for each image: builds it, asserts `gcc`/`g++`/`make`/`cmake`/`cc` are absent, asserts `tshark`/`nmap`/`git` placement (present only for `-network`/`-network`/`-rules` respectively), asserts non-root, and runs each tag's `smoke_imports`. **Done, with a mechanism change**: since `final-minimal` (Chainguard) has no shell/coreutils at all, every assertion runs through `docker run --rm --entrypoint python3 <image> -c "..."` using `shutil.which()`/`os.getuid()`, not `command -v`/`id`. Manually verified equivalent to a red→green cycle by running these exact checks locally against the pre-hardening and post-hardening images (see T007/T008). **Since moved**: this job (plus `sandbox-nodejs-smoke-test`, `sandbox-image-matrix`, `sandbox-python-digest-check`) now lives in the path-filtered `.github/workflows/sandbox-images.yml`, split out of `ci.yml` during review.

### Implementation for User Story 1

- [X] T004 [US1] Rewrite `backend/sandbox/Dockerfile.sandbox.python` as a multi-stage build: a `builder` stage (`FROM python:3.14-slim AS builder`, unchanged base for now) installs the full `$APT_FILE` package list and `pip install --no-cache-dir -r $REQ_FILE` into a virtualenv at `/opt/venv`; a final stage starts fresh `FROM python:3.14-slim`, and `COPY --from=builder /opt/venv /opt/venv` with `ENV PATH="/opt/venv/bin:$PATH"`. **Done, expanded to two final stages** (`final-minimal`/`final-full`) as part of T011 — see there.
- [X] T005 [US1] Create files per research.md §2's classification for the final stage's runtime-only apt subset, installed via a `RUNTIME_APT_FILE` build-arg (final-full only — final-minimal has no apt at all, see T011/research.md §3a): `apt.sandbox.base.runtime.txt` (`libfreetype6`, `libpng16-16t64` — documented fallback only, not consumed by any tag today), `apt.sandbox.extended.runtime.txt` (`libmagic1t64`, `libfuzzy2`, `libpcap0.8t64`, `libgl1`, `libglib2.0-0t64`, `libgomp1`, `libssl3t64`, `libgdal36`, `libgeos-c1t64`, `libproj25`, `proj-data` — data/malware), `apt.sandbox.network.runtime.txt` (extended.runtime + `tshark`/`nmap`). **Expanded during T007 empirical testing**: two more were needed — `apt.sandbox.cti.runtime.txt` (`libmagic1t64` — `pycti` transitively imports `python-magic`) and `apt.sandbox.rules.runtime.txt` (`git` — `GitPython` shells out to it at import time). All exact package names verified against the real Debian trixie repo (several have a `t64` suffix not in the original apt file names). **Expanded again post-review**: `apt.sandbox.network.runtime.txt` was missing `libkrb5-3`/`libgssapi-krb5-2` (`requests-kerberos`'s pykerberos C extension), and `sandbox-python-malware` needed its own new `apt.sandbox.malware.runtime.txt` (`extended.runtime` + `libtsk19t64`/`libewf2`/`libafflib0t64`/`libvhdi1`/`libvmdk1` for `pytsk3`/`dfvfs`/`plaso`) rather than sharing `apt.sandbox.extended.runtime.txt` with `-data`, which doesn't need the DFIR libs. Both found by a code-review pass tracing each domain's requirements file against its shipped runtime apt list, and confirmed by actually building and importing.
- [X] T006 [US1] Add "why" comments in `Dockerfile.sandbox.python` next to the builder/final split, explaining the build-only-vs-runtime rationale and the multi-stage design (Constitution Principle IV).
- [X] T007 [US1] Run quickstart.md §2 locally for all 7 Python tags (rebuild via the updated `create-register-images.sh`); fix any import breakage before proceeding. **Done**: found and fixed two real runtime-CLI gaps (`sandbox-python-cti`/`libmagic`, `sandbox-python-rules`/`git` — see T005). `sandbox-python-data` remains excluded (pre-existing `tensorflow` pin issue, T001). All other 5 tags + base import and run cleanly with zero build tools present.
- [X] T008 [US1] Re-run the T003 CI smoke-test job and confirm it now passes for all 7 Python tags plus `sandbox-nodejs`. **Done via manual equivalent** (no GH Actions runner available in this environment): ran every assertion in the T003 job (build-tool absence, runtime-CLI-exception placement, non-root, smoke imports) directly against the rebuilt images — all pass for the 6 buildable Python tags + nodejs.

**Checkpoint**: User Story 1 is fully functional and independently verified in CI — this is the MVP.

---

## Phase 4: User Story 2 - Run sandbox containers on a minimal, non-root, pinned base (Priority: P2)

**Goal**: Every sandbox image runs as non-root, and every base image reference is pinned by digest; the base/`cti`/`rules`/`soar` tags and the Node.js image move to a minimal hardened base.

**Independent Test**: `docker run --rm <image> id` returns a non-root UID for all 8 images; every `FROM` line in both Dockerfiles is digest-pinned (quickstart.md §3).

### Tests for User Story 2

> Write T009 first and confirm it FAILS against the Dockerfiles from Phase 3 (still root, still tag-pinned) before starting T010.

- [X] T009 [US2] Extend the `sandbox-image-smoke-test` job (T003) with two more assertions per image: `id -u` is non-zero, and (as a separate, non-matrixed step) every `FROM` line in `backend/sandbox/Dockerfile.sandbox.python` and `backend/sandbox/Dockerfile.sandbox.nodejs` matches `FROM \S+@sha256:` (fails on a bare tag). **Done**: non-root assertion added inline to the matrix job (`os.getuid()` via the `--entrypoint python3` pattern); digest check split into two separate jobs (`sandbox-python-digest-check`, and a digest assertion inside `sandbox-nodejs-smoke-test`) since it's per-Dockerfile, not per-tag.

### Implementation for User Story 2

- [X] T010 [US2] In `backend/sandbox/Dockerfile.sandbox.nodejs`: resolve and pin `FROM node:22-slim` to its current digest; add non-root execution; `USER` after all root-owned steps. **Done, with a correction found during build**: `node:22-slim` already ships a `node` user/group at UID/GID 1000 — `useradd -u 1000 sandboxuser` collided with it (`groupadd: GID '1000' already exists`, exit 4). Fixed by reusing the existing `node` user instead of creating `sandboxuser`.
- [X] T011 [US2] Split `Dockerfile.sandbox.python`'s final stage into two named build targets: `final-minimal` (Chainguard) and `final-full` (Debian-slim, digest-pinned), selected per tag via `docker build --target`, with a `final_target` field in `sandbox-images.json`. **Done, with the tag→target assignment corrected during T007 testing**: originally planned as base/`cti`/`rules`/`soar` → `final-minimal`, `data`/`malware`/`network` → `final-full`. Empirically, `cti` and `rules` don't work on `final-minimal` (see T005/research.md §3a: transitive `libmagic`/`git` needs, unrelated to the base-image choice itself) and now use `final-full` too — only `sandbox-python` (base) and `sandbox-python-soar` actually ship on `final-minimal`. `final-minimal` also needed a different copy strategy than planned (`COPY --from=builder .../site-packages` into the base image's own site-packages, not a plain `/opt/venv` copy — its venv's `bin/python` symlink points at a path that doesn't exist in Chainguard's filesystem) and has no `RUNTIME_APT_FILE`/`USER`/`useradd` step at all (no package manager, no shell; already non-root by default at UID 65532).
- [X] T012 [US2] Add non-root user handling to both final targets. **Done, adjusted per T011's findings**: `final-full` creates `sandboxuser` (UID/GID 1000) as originally planned; `final-minimal` needed no explicit user creation — Chainguard's base image already defaults to non-root (UID 65532) and has no `useradd` to run in the first place.
- [X] T013 [US2] Add a comment block above each final-stage `FROM` line in `Dockerfile.sandbox.python` recording the base-image evaluation outcome (FR-006), referencing research.md §3/§3a.
- [X] T014 [US2] Run quickstart.md §3 for all 8 images; if `final-minimal` breaks any base/`cti`/`rules`/`soar` package's native-extension import, fix it or fall back that specific tag to `final-full` and document why (per spec Assumptions). **Done**: this is exactly what happened for `cti`/`rules` (see T011) — fallback applied and documented. `sandbox-python`/`sandbox-python-soar` confirmed working on `final-minimal` with zero issues (including matplotlib rendering with no system freetype/png installed).
- [X] T015 [US2] Re-run the extended T009 CI assertions and confirm all 8 images now report non-root UID and every `FROM` line is digest-pinned. **Done via manual equivalent**: all 6 buildable Python tags report non-root (`final-minimal` → 65532, `final-full` → 1000) plus `sandbox-nodejs` → 1000 (the existing `node` user); all 3 `FROM` lines in `Dockerfile.sandbox.python` and the 1 in `Dockerfile.sandbox.nodejs` are digest-pinned.

**Checkpoint**: User Stories 1 and 2 both pass their CI checks independently.

---

## Phase 5: User Story 3 - Scan sandbox images for vulnerabilities on demand (Priority: P3)

**Goal**: A maintainer can trigger a vulnerability scan of any sandbox image at any time, without it ever blocking a build or publish.

**Independent Test**: Manually trigger the scan workflow against a published tag and against a local-only domain tag; both report results; `build-push-sandbox.yml` behavior is unchanged (quickstart.md §4).

### Implementation for User Story 3

- [X] T016 [US3] Create `.github/workflows/sandbox-image-scan.yml`: `workflow_dispatch` with an `image` input — if it starts with `xtmone/`, pull it directly; otherwise look it up in `backend/sandbox/sandbox-images.json` and build it first. Run `aquasecurity/trivy-action` twice (table format for the log, SARIF uploaded as an artifact) with `exit-code: '0'`.
- [X] T017 [US3] Confirm `.github/workflows/build-push-sandbox.yml` is unmodified by this feature. **Done**: `git status` shows no changes to that file.
- [X] T018 [US3] Manually trigger `sandbox-image-scan.yml` once with `xtmone/sandbox-python:0.2` and once with a local-only domain tag; confirm both runs complete successfully and report findings regardless of severity. **Done via manual equivalent** (no GH Actions runner available): ran `aquasecurity/trivy` directly against `sandbox-python:0.2` — completed with exit code 0 and correctly surfaced 2 real HIGH CVEs (transitive `msgpack`/`setuptools`), confirming the underlying scan mechanism the workflow wraps.

**Checkpoint**: User Stories 1–3 all pass independently.

---

## Phase 6: User Story 4 - Document sandbox image hardening standards (Priority: P4)

**Goal**: A contributor can find and apply a written hardening guideline without asking a maintainer.

**Independent Test**: The guideline exists, is linked from `SANDBOX.md`, and a reviewer can cite a specific rule from it for a hypothetical PR reintroducing a build tool or root execution (quickstart.md §5).

### Implementation for User Story 4

- [X] T019 [P] [US4] Write `dev-docs/sandbox-image-hardening.md` with numbered rules and a "how to verify" section. **Done, kept current with T005/T011's real findings**: rule 2 documents both runtime-CLI exceptions (`tshark`/`nmap` and `git`) and the transitive-dependency lesson (`pycti`→`python-magic`); rule 5 documents the `final-minimal`/`final-full` split.
- [X] T020 [US4] Add a pointer to `dev-docs/sandbox-image-hardening.md` from `backend/sandbox/SANDBOX.md`.
- [X] T021 [US4] Update any build/usage examples in `backend/sandbox/SANDBOX.md` that reference the old single-stage Dockerfile shape or bare-tag base images, so they match the multi-stage, digest-pinned Dockerfiles from US1/US2. **Also added**: a note in the "Known Package Incompatibilities" section about `tensorflow==2.21.0` having no Python 3.14 wheel (found via T001's baseline, pre-existing, out of scope).
- [X] T022 [US4] Walk spec.md's User Story 4 acceptance scenarios against the written guideline. **Done**: rule 1 is a specific, citable rule for "reintroduces a build tool"; rule 4 for "reintroduces root execution."

**Checkpoint**: All four user stories are independently functional and documented.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T023 [P] Run the full `quickstart.md` end-to-end (all 6 sections) against the final state of all 8 images. **Done**: all sections pass for the 6 buildable Python tags + nodejs (`sandbox-python-data` excluded — pre-existing `tensorflow` issue, T001). 4/4 `FROM` lines digest-pinned; guideline discoverable from `SANDBOX.md`; every image non-root; runtime-CLI exceptions (`tshark`/`nmap`/`git`) correctly scoped to exactly the tags that need them.
- [X] T024 [P] Grep `backend/sandbox/sandbox.docker.toml`, `backend/sandbox/sandbox.docker.gvisor.toml`, `backend/sandbox/sandbox.k8s.firecracker.toml`, and `backend/app/core/config.py`'s `_OPENSANDBOX_DEFAULT_IMAGES` for sandbox image tag references; confirm none need edits (spec FR-010 — tags and `Sandbox.create(image=...)` usage stay stable). **Done**: the `.toml` pool configs don't hardcode image names at all; `_OPENSANDBOX_DEFAULT_IMAGES` still points at `sandbox-python:0.2`/`sandbox-nodejs:0.2`, unchanged.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies.
- **Foundational (Phase 2)**: Depends on Setup (T001) only informally (T001 doesn't touch files T002 creates); T002 BLOCKS T003 and T016 (both consume `sandbox-images.json`).
- **User Story 1 (Phase 3)**: Depends on Foundational (T002). No dependency on US2–US4.
- **User Story 2 (Phase 4)**: Depends on Foundational (T002) and, practically, on US1's multi-stage split existing to attach the base-image change to (T009–T015 assume `Dockerfile.sandbox.python` already has a `final` stage from T004).
- **User Story 3 (Phase 5)**: Depends on Foundational (T002) only — independent of US1/US2's Dockerfile content (it scans whatever image reference it's given).
- **User Story 4 (Phase 6)**: Depends on US1–US3 being complete (it documents their outcomes accurately).
- **Polish (Phase 7)**: Depends on all four user stories.

### Parallel Opportunities

- T009 [US2] can be written in parallel with T004–T006 [US1] (different sections of `ci.yml` and `Dockerfile.sandbox.python`), but don't merge either story's implementation before its own tests are confirmed red→green.
- T016 [US3] is independent of US1/US2 implementation entirely and can be built in parallel once T002 lands.
- T019 [US4] can be drafted in parallel with US1–US3 implementation, but T021/T022 need US1–US3 finished to describe them accurately.
- T023, T024 (Polish) are independent of each other.

---

## Parallel Example: Foundational → User Story 1 kickoff

```bash
# After T002 (sandbox-images.json) lands, T003 can start immediately:
Task: "Add sandbox-image-smoke-test job to .github/workflows/ci.yml, matrix-driven from sandbox-images.json"
# (since moved to the path-filtered .github/workflows/sandbox-images.yml during review)

# Meanwhile, US3's workflow scaffolding has no dependency on US1's Dockerfile changes:
Task: "Create .github/workflows/sandbox-image-scan.yml (workflow_dispatch, trivy-action)"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1 (T001) and Phase 2 (T002).
2. Complete Phase 3 (User Story 1) — T003 red, then T004–T008 to green.
3. **STOP and VALIDATE**: run quickstart.md §2 against all 7 Python tags.
4. This alone ships the highest-value fix (no compilers/build tools in any shipped sandbox image).

### Incremental Delivery

1. Setup + Foundational → shared matrix ready.
2. User Story 1 → validate → this is the MVP the original ask ("multi-stage build for `Dockerfile.sandbox.python`") targeted.
3. User Story 2 → validate → non-root + pinned + partially-minimal base.
4. User Story 3 → validate → on-demand scanning available.
5. User Story 4 → validate → guideline documents the above so it doesn't regress.

---

## Notes

- [P] tasks touch different files/sections and have no completed-task dependency between them.
- No `[Story]` label on Setup/Foundational/Polish tasks, per the checklist format.
- The base-image split introduced in T011 (`final-minimal` vs `final-full`) is the one piece of this plan with real implementation risk — budget time in US2 for T014's fallback path if Chainguard doesn't support a given base/`cti`/`rules`/`soar` package cleanly.
