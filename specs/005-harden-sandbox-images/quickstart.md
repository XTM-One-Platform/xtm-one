# Quickstart: Validating the Hardened Sandbox Images

Prerequisites: Docker with Buildx installed locally; run all commands from the repository root.

**Invocation note**: `final-minimal` (Chainguard, used only by `sandbox-python`
and `sandbox-python-soar`) has **no shell and no coreutils** — `which`, `id`,
`sh -c` all fail outright there. Every check below instead runs through the
image's own interpreter via `docker run --rm --entrypoint python3 <image> -c
"..."`, which works uniformly across `final-minimal` and `final-full` (the
Debian-based target used by every other tag — `-cti`/`-data`/`-malware`/
`-network`/`-rules` — see `research.md` §3a for why `-cti`/`-rules` ended up
here too).

## 1. Build the hardened images

Each tag's `--target` (`final-minimal` only for `sandbox-python`/`sandbox-python-soar`; `final-full` for every other tag, including `-cti`/`-rules`) and `RUNTIME_APT_FILE` (its final-stage-only apt list) come from `sandbox-images.json`, the single source of truth for the per-tag build matrix. `build-image.sh` is the one script that reads it and runs the matching `docker build` — used here, by `create-register-images.sh`, and by the CI/scan workflows, so there's exactly one place that constructs this invocation.

```bash
cd backend/sandbox

# Base Python image (the one actually CI-published) — final-minimal
./build-image.sh sandbox-python:0.2

# The 6 domain images (local dev only — see create-register-images.sh)
./build-image.sh sandbox-python-cti:0.2
./build-image.sh sandbox-python-data:0.2
./build-image.sh sandbox-python-malware:0.2
./build-image.sh sandbox-python-network:0.2
./build-image.sh sandbox-python-rules:0.2
./build-image.sh sandbox-python-soar:0.2

# Node.js image
./build-image.sh sandbox-nodejs:0.2
```

Or simply run `./create-register-images.sh <version>` from `backend/sandbox/`, which builds all 7 Python images plus the Node.js image in one go (and imports them into local k3s if available).

Expected: all 8 builds succeed (the `apt.sandbox.extended.txt`-based ones — `data`/`malware`/`network` — noticeably longer due to their larger package sets); no build errors from missing runtime shared libraries. **Known exception**: `sandbox-python-data` currently fails at its dependency install — `tensorflow==2.21.0` has no published wheel for Python 3.14 at all. This is a pre-existing pin issue unrelated to this feature's hardening changes (verified against the unmodified Dockerfile too); see `SANDBOX.md`'s "Known Package Incompatibilities" section.

## 2. Verify User Story 1 — no build-only tooling in the final image

```bash
# None of these should print True in ANY of the 7 sandbox-python* images
for image in sandbox-python sandbox-python-cti sandbox-python-data sandbox-python-malware \
             sandbox-python-network sandbox-python-rules sandbox-python-soar; do
  docker run --rm --entrypoint python3 "$image:0.2" -c "
import shutil
bad = [b for b in ('gcc','g++','cc','make','cmake') if shutil.which(b)]
print('$image:', 'FAIL - build tools present:' , bad if bad else 'ok, none present')
"
done
```

Then confirm every previously-supported package still imports per image (see spec Acceptance Scenario US1-2):

```bash
docker run --rm --entrypoint python3 sandbox-python:0.2 -c "import numpy, pandas, matplotlib, scipy, tabulate; print('base OK')"
docker run --rm --entrypoint python3 sandbox-python-cti:0.2 -c "import pymisp, pycti, stix2, taxii2client; print('cti OK')"
docker run --rm --entrypoint python3 sandbox-python-network:0.2 -c "import scapy.all, pyshark, libnmap, impacket; print('network OK')"
docker run --rm --entrypoint python3 sandbox-python-malware:0.2 -c "import yara, pefile, lief, oletools; print('malware OK')"
docker run --rm --entrypoint python3 sandbox-python-data:0.2 -c "import sklearn, pandas, geopandas; print('data OK (tensorflow excluded — see step 1 note)')"
docker run --rm --entrypoint python3 sandbox-python-rules:0.2 -c "import sigma, jsonschema, pydantic; print('rules OK')"
docker run --rm --entrypoint python3 sandbox-python-soar:0.2 -c "import thehive4py, jira, boto3; print('soar OK')"
```

`pyshark` importing successfully is the concrete check for the Clarifications' runtime-exception decision, scoped to `sandbox-python-network` only — confirm the `tshark`/`nmap` binaries themselves follow the same scoping:

```bash
for image in sandbox-python sandbox-python-cti sandbox-python-data sandbox-python-malware \
             sandbox-python-rules sandbox-python-soar; do
  docker run --rm --entrypoint python3 "$image:0.2" -c "
import shutil
present = [t for t in ('tshark','nmap') if shutil.which(t)]
print('$image:', 'FAIL - present:' if present else 'ok, absent', present)
"
done
docker run --rm --entrypoint python3 sandbox-python-network:0.2 -c "
import shutil
missing = [t for t in ('tshark','nmap') if not shutil.which(t)]
print('FAIL - missing:' if missing else 'ok, both present', missing)
"
```

## 3. Verify User Story 2 — non-root, pinned base

```bash
for image in sandbox-python sandbox-python-cti sandbox-python-data sandbox-python-malware \
             sandbox-python-network sandbox-python-rules sandbox-python-soar; do
  docker run --rm --entrypoint python3 "$image:0.2" -c "import os; print('$image uid:', os.getuid())"     # expect non-zero
done
docker run --rm --entrypoint node sandbox-nodejs:0.2 -e "console.log('nodejs uid:', process.getuid())"

grep -n "^FROM" backend/sandbox/Dockerfile.sandbox.python backend/sandbox/Dockerfile.sandbox.nodejs
# expect every FROM line — builder, final-minimal, AND final-full — to include @sha256:..., not a bare tag
```

Rebuild step 1's images if you haven't already run them with `--target`/`RUNTIME_APT_FILE` since Dockerfile.sandbox.python was split — the non-root/digest checks above are only meaningful against images actually built with the correct per-tag `--target`.

## 4. Verify User Story 3 — on-demand, non-blocking scan

Locally (approximates the `sandbox-image-scan.yml` workflow):

```bash
trivy image sandbox-python-data:0.2   # or any other locally-built/pulled tag
```

Expected: a report of OS/dependency CVEs by severity; running it (or not) has no bearing on whether step 1's builds succeeded — confirming the scan is independent of the build/publish path (spec FR-008).

In CI: trigger `sandbox-image-scan.yml` via `workflow_dispatch` with either a Docker-Hub-published reference (`xtmone/sandbox-python:0.2`) or a domain-image tag to build-then-scan, and confirm the run completes and reports results regardless of findings (exit code 0 either way).

## 5. Verify User Story 4 — hardening guideline is discoverable and actionable

```bash
test -f dev-docs/sandbox-image-hardening.md && echo "ok: guideline exists"
grep -n "sandbox-image-hardening" backend/sandbox/SANDBOX.md
```

Manually confirm the guideline states, in reviewer-citable form: no build tools in final images, documented exceptions required for any runtime CLI tool, pinned digests, non-root execution, and how to run the checks above.

## 6. Regression check — existing OpenSandbox usage unaffected

```bash
grep -n "sandbox-python\|sandbox-nodejs" backend/sandbox/sandbox.docker.toml backend/sandbox/sandbox.docker.gvisor.toml backend/sandbox/sandbox.k8s.firecracker.toml
grep -n "_OPENSANDBOX_DEFAULT_IMAGES" backend/app/core/config.py
```

Expected: no image tag references in the pool `.toml` configs at all (they don't hardcode image names), and `_OPENSANDBOX_DEFAULT_IMAGES` still points at the `sandbox-python`/`sandbox-nodejs` images (now `:0.3`) — no config edits required by this feature (spec FR-010).
