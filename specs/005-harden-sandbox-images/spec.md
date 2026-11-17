# Feature Specification: Harden OpenSandbox Docker Images

**Feature Branch**: `005-harden-sandbox-images`

**Created**: 2026-08-10

**Status**: Draft

**Input**: User description: "security(sandbox): harden OpenSandbox Docker images (base image, build-only apt packages, review guidelines)"

## Clarifications

### Session 2026-08-10

- Q: `pyshark` and `python-libnmap` in the extended requirement catalog wrap the `tshark`/`nmap` CLI tools and need them at runtime, but User Story 1 proposed removing those tools entirely — how should this conflict be resolved? → A: Keep `tshark`/`nmap` in the **extended** final image only, as documented runtime dependencies of `pyshark`/`python-libnmap`; the **base** image and the Node.js image stay fully free of them.
- Q: Should a vulnerability scan finding block the sandbox image build/publish, or only report? → A: Run scan on demand only — scanning is a manually-triggered check, not wired into the build/publish pipeline as a gate, and not run on an automatic recurring schedule.
- Q: `Dockerfile.sandbox.python` is actually built into 7 tags today (`sandbox-python:0.2` plus 6 domain images — `-cti`, `-data`, `-malware`, `-network`, `-rules`, `-soar` — via `create-register-images.sh`), not just one "base" and one "extended" as originally described; only `sandbox-python` and `sandbox-nodejs` are published to Docker Hub by CI today. How should this feature's verification scope the other 6? → A: Cover all 7 Python tags in the new CI build/smoke-test coverage (since they share one Dockerfile, hardening it affects all of them); leave Docker Hub publishing exactly as it is today (still only `sandbox-python`/`sandbox-nodejs`) — publishing the other 6 is out of scope for this feature.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Ship sandbox images without build-only tooling (Priority: P1)

As a platform security engineer, I want the sandbox container images that execute agent-generated and user-defined code to ship without compilers, build toolchains, or network reconnaissance utilities, so that if a container-boundary escape ever occurs, the attacker does not inherit a ready-made toolkit for further compromise.

**Why this priority**: This is the highest-impact, lowest-effort change. The images currently ship `gcc`/`g++`/`make`/`cmake`/`*-dev` headers and `openssh-client` that are only needed to build Python dependencies, never to run them. Removing them shrinks the blast radius of an escape without touching runtime isolation.

**Independent Test**: Build `sandbox-python:0.2` and each of the six domain images (`sandbox-python-{cti,data,malware,network,rules,soar}:0.2`) and inspect the resulting image filesystem/package list. It can be shipped and verified independently of the base-image and CI-scanning work.

**Acceptance Scenarios**:

1. **Given** the `base` sandbox Python image build, **When** the build completes, **Then** the final image contains none of `gcc`, `make`, `cmake`, or other build-only packages, while `numpy`, `pandas`, `matplotlib`, `scipy`, and `tabulate` remain importable and functional.
2. **Given** each of the six domain sandbox Python image builds (`sandbox-python-cti`, `-data`, `-malware`, `-network`, `-rules`, `-soar`, built by `create-register-images.sh`), **When** the build completes, **Then** the final image contains none of `gcc`, `g++`, `make`, `cmake`, `python3-dev`, `*-dev` headers, or `openssh-client`, while every package in that domain's requirement file that was previously installable still installs and imports successfully — including, for `sandbox-python-network`, `pyshark` and `python-libnmap`, whose `tshark`/`nmap` runtime binaries are retained (see Clarifications).
3. **Given** a package in the extended catalog that needs a runtime shared library (e.g. `libmagic1`, `libpcap0.8`, `libgomp1`), **When** the final image is built, **Then** that runtime library is still present, only its `-dev`/build counterpart is removed.

---

### User Story 2 - Run sandbox containers on a minimal, non-root, pinned base (Priority: P2)

As a platform security engineer, I want the sandbox images built from a minimal, non-root base image that is pinned to an immutable version, so that the OS-level attack surface and the privilege an escaping process inherits are both reduced, and image contents can't silently drift.

**Why this priority**: Builds on User Story 1's multi-stage split (a minimal final stage is easiest to introduce once build and runtime concerns are already separated). It's a larger investment because it may require re-validating every dependency across the domain requirement files against a different base OS.

**Independent Test**: Build the sandbox images against the chosen minimal base and confirm each image runs as a non-root user and that its base layer is referenced by digest rather than a floating tag.

**Acceptance Scenarios**:

1. **Given** the sandbox Python and Node.js images, **When** built, **Then** their base image reference is pinned to a specific immutable digest rather than a mutable tag like `slim`.
2. **Given** a running sandbox container, **When** code is executed via `sandbox.commands.run()`, **Then** the process runs as a non-root user.
3. **Given** the switch to a hardened/minimal base, **When** the existing base and each domain's dependency set are installed, **Then** all previously-working functionality (imports, native extensions such as `numpy`, `scapy`, `lxml`) continues to work, or any package that cannot be supported is explicitly documented as dropped.

---

### User Story 3 - Scan sandbox images for vulnerabilities on demand (Priority: P3)

As a maintainer, I want to be able to trigger a vulnerability scan of a sandbox image on demand, so that I can check its current CVE exposure — including CVEs disclosed after the image was built, with no code change needed — without scanning being forced into every build.

**Why this priority**: Provides on-demand assurance once the image content itself has been reduced and hardened (User Stories 1–2); checking what still exists is more valuable after the surface has already been minimized.

**Independent Test**: Manually trigger the scan against a built sandbox image and confirm the results (severity, affected package) are surfaced to the person who ran it.

**Acceptance Scenarios**:

1. **Given** a built sandbox image (`sandbox-python:0.2`, any of the six `sandbox-python-{cti,data,malware,network,rules,soar}:0.2` domain images, or `sandbox-nodejs`), **When** a maintainer manually triggers a scan, **Then** the image is scanned for known OS and dependency vulnerabilities and the results are reported.
2. **Given** a sandbox image already published, **When** a new CVE is disclosed in one of its packages after the fact, **Then** a maintainer can trigger a scan at any time to detect it, without needing a new commit.
3. **Given** a scan finds a vulnerability, **When** the scan completes, **Then** the severity and affected package are visible to the maintainer who triggered it.
4. **Given** a routine sandbox image build/publish, **When** the pipeline runs, **Then** it completes without requiring a scan to run or pass — scanning is a separate, manually-invoked check, not a build gate.

---

### User Story 4 - Document sandbox image hardening standards (Priority: P4)

As a contributor changing a sandbox image in the future, I want a written hardening guideline for these images, so I know what's expected (no build tools in final images, non-root, pinned digests, scanning) and don't unknowingly regress the posture established by this feature.

**Why this priority**: Lowest urgency — it codifies and preserves the outcomes of User Stories 1–3 rather than changing the images itself, and depends on the concrete choices made in those stories to be worth documenting accurately.

**Independent Test**: A new contributor (or reviewer) can read the guideline and, using only it, determine whether a proposed change to a sandbox Dockerfile complies or regresses hardening.

**Acceptance Scenarios**:

1. **Given** the completed hardening work from User Stories 1–3, **When** a contributor looks for guidance on modifying a sandbox image, **Then** a discoverable document describes the required practices (multi-stage builds, no build tools in final image, non-root, pinned digests, scanning expectations).
2. **Given** the documentation, **When** a reviewer checks a pull request that adds a new package to a sandbox image, **Then** the reviewer can point to a specific documented rule to request changes if the PR would reintroduce a build tool or root execution into the final image.

### Edge Cases

- What happens when a package in the extended catalog has no pre-built wheel and genuinely requires a compiler at *install* time — how is it still installed if the final image has no compiler? (Expected: compiled in a build stage, only the resulting artifact is copied into the final image.)
- `tshark`/`nmap` are retained in the `sandbox-python-network` image specifically because `pyshark`/`python-libnmap` shell out to them at runtime (see Clarifications); this is a documented exception, not a gap — any future addition of a package that needs a new runtime CLI tool must document that need explicitly rather than being assumed build-only.
- What happens if an on-demand scan finds a CRITICAL CVE with no available fix in the pinned base image? (Expected: it is reported to the maintainer who triggered it; remediation/risk-acceptance is a manual follow-up decision, since scanning does not gate the pipeline.)
- How does the system handle a future dependency that only publishes packages for the previous (non-hardened) base OS family, if the base image family changes?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The sandbox Python image build process MUST separate dependency compilation from the final runtime image, such that compilers and build toolchains used only to build/install dependencies are not present in the image that executes user/agent code.
- **FR-002**: The final `sandbox-python` (base) image and the Node.js image, and the `sandbox-python-cti`/`-data`/`-malware`/`-rules`/`-soar` domain images, MUST NOT contain network reconnaissance or packet-capture tooling (e.g. `nmap`, `tshark`). Only `sandbox-python-network` MAY retain `nmap`/`tshark`, solely as documented runtime dependencies of `python-libnmap`/`pyshark`. No domain image may retain compilers, build toolchains, or `*-dev` headers.
- **FR-003**: Every Python package currently installable via `requirements.sandbox.base.txt` and each domain requirement file (`requirements.sandbox.{cti,network,malware,soar,data,rules}.txt`) MUST remain installable and importable in its corresponding image after build-only packages are removed from the final image, or its removal MUST be explicitly documented.
- **FR-004**: Sandbox containers MUST execute user/agent-submitted code as a non-root user.
- **FR-005**: The base image used by each sandbox Dockerfile MUST be referenced by an immutable digest rather than a floating tag.
- **FR-006**: The project MUST evaluate at least one minimal/hardened base image family as the sandbox images' foundation and record the outcome of that evaluation (adopted, or explicitly rejected with reasons).
- **FR-007**: The project MUST provide a way for a maintainer to manually trigger a vulnerability scan of a sandbox image (OS and dependency-level) at any time, independent of a code change or new build.
- **FR-008**: Vulnerability scanning MUST NOT be a required, blocking step of the standard sandbox image build/publish pipeline — a routine build/publish MUST succeed without a scan running.
- **FR-009**: The project MUST publish a written guideline describing the hardening practices expected of sandbox images (no build tools in final image, non-root execution, pinned digests, scanning expectations), discoverable by contributors alongside the sandbox Dockerfiles.
- **FR-010**: Existing OpenSandbox functionality (image build commands documented for the base Python image, each domain Python image, and the Node.js image) MUST continue to work after hardening changes, with no change required to how a client requests an image (`Sandbox.create(image=...)`).

### Key Entities

- **Sandbox image variant**: A named, buildable Docker image that OpenSandbox instantiates to run untrusted code, distinguished by which OS packages and Python/Node dependencies it ships. Eight tags exist today, all built from just two Dockerfiles: `sandbox-python:0.2` (base) and `sandbox-nodejs:0.2` (CI-published to Docker Hub via `build-push-sandbox.yml`, and the two defaults in `OPENSANDBOX_IMAGES`), plus six domain variants — `sandbox-python-{cti,data,malware,network,rules,soar}:0.2` — built locally by `create-register-images.sh` for k3s dev only, not currently CI-published.
- **Build-only dependency**: A package (compiler, header, build tool) needed only to compile or install another dependency, never to execute already-installed code. `nmap`/`tshark` in the `sandbox-python-network` image are explicitly excluded from this category — they are runtime dependencies of `python-libnmap`/`pyshark`.
- **Hardening guideline**: A documented set of rules contributors must follow when changing a sandbox image definition.
- **Vulnerability scan result**: A record of known CVEs found in a sandbox image at the time a maintainer manually triggered a scan, including affected package and severity.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Each of the 7 `sandbox-python*` image tags contains zero build-only packages (compilers, `*-dev` headers, build tools) while 100% of that image's previously-supported requirement-file packages remain installable and importable.
- **SC-002**: 100% of sandbox images (all 7 `sandbox-python*` tags plus `sandbox-nodejs`) run their default process as a non-root user.
- **SC-003**: 100% of sandbox image base references are pinned to an immutable digest, eliminating unreviewed drift from upstream tag updates.
- **SC-004**: A maintainer can obtain a current vulnerability scan result for any sandbox image — published (`sandbox-python`, `sandbox-nodejs`) or locally-built-only (the six domain images) — on demand, without needing a new commit, and without that scan ever blocking a routine build/publish.
- **SC-005**: A contributor unfamiliar with the hardening work can locate and apply the documented guideline without needing to ask a maintainer, verified by a new sandbox-image-touching pull request citing the guideline in review.

## Assumptions

- The runtime isolation layer (Docker vs. gVisor/Firecracker/k8s pool) is out of scope for this feature; it has already been evaluated separately, and this feature only addresses what is baked into the image itself.
- Whether Filigran has an existing company-wide container-hardening standard (from OpenCTI, OpenAEV, etc.) that should be adopted instead of a bespoke one cannot be determined from this repository; this feature will produce a self-contained guideline and note it as superseded if a company-wide standard is later found to apply.
- "Minimal/hardened base image" evaluation (User Story 2) may conclude that no candidate base fully supports the extended package catalog (e.g. DFIR/geospatial packages with complex native dependencies); in that case, the extended variant may retain a fuller base while the default/base variant adopts the minimal one, and this trade-off is documented rather than silently dropped.
- "Non-root by default" applies to the process that executes user/agent code; it does not preclude a root-owned build stage internal to the multi-stage Dockerfile.
- Existing consumers of these images (OpenSandbox pool configs and `_OPENSANDBOX_DEFAULT_IMAGES`, referencing `sandbox-python:0.2` and `sandbox-nodejs:0.2`) are unaffected by internal image content/base changes as long as image tags and `Sandbox.create(image=...)` usage remain stable.
- User Story 3 deliberately scopes vulnerability scanning to on-demand/manual triggering rather than the automatic CI-gate-plus-recurring-schedule originally proposed; if automatic/scheduled scanning is wanted later, it can be layered on without changing User Stories 1, 2, or 4.
