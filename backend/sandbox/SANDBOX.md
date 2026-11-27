# Python Sandbox Images — Design Notes

## Overview

The sandbox directory contains everything needed to build Python Docker images
for use with OpenSandbox (`commands.run()` mode). Images are parameterised:
one `Dockerfile.sandbox.python` builds any flavour via `--build-arg`/`--target`.

**Hardening**: both Dockerfiles are digest-pinned and non-root.
`Dockerfile.sandbox.python` is also multi-stage (builder + final-full/
final-minimal), separating build-only tooling from the shipped image;
`Dockerfile.sandbox.nodejs` is a single stage — it installs no extra
packages today, so there's nothing yet to split out. See
[`dev-docs/sandbox-image-hardening.md`](../../dev-docs/sandbox-image-hardening.md)
for the rules and how to verify a change complies before opening a PR.

---

## File Structure

```
sandbox/
├── Dockerfile.sandbox.python          # Multi-stage, parameterised Dockerfile (builder + final-minimal + final-full)
├── Dockerfile.sandbox.nodejs          # Node.js sandbox (separate)
├── Dockerfile.sandbox.dev             # Dev images for sandbox_run_command: python-dev (backend uv.lock, dev group) and nodejs-dev (frontend yarn.lock in the yarn cache, plus a bare CPython without pip for the workspace helper), built from the repository root by build-dev-image.sh
├── Dockerfile.sandbox.toolchain       # Generic toolchain images for any repository (no lock baked; the checkout installs its dependencies): toolchain-python (3.14 + 3.12, pip, uv, poetry, pytest, pytest-asyncio), toolchain-node (Node 24 + 26, npm, corepack yarn/pnpm), toolchain-jvm (JDK 21, Maven 3.9), toolchain-rust (stable, clippy, rustfmt; CARGO_TARGET_DIR outside the workspace), toolchain-dbt (dbt-core + dbt-postgres); behind AGENTIC_CODING only
├── registry-mirror/nginx.conf         # Read-only npm, PyPI and Maven Central mirror proxy of the command containers (SANDBOX_REGISTRY_MIRROR_URL): GET/HEAD only, no credential forwarded, its security boundary in the file header
├── build-dev-image.sh                 # Build one dev or toolchain image, `<target> <image>:<tag>`, from the repository root
├── build-image.sh                     # Build one image, `<image>:<tag>` (docker build invocation shared by create-register-images.sh + CI + the scan workflow)
├── create-register-images.sh          # Build + import all images into k3s as `<image>:<version>`, driven by sandbox-images.json
├── sandbox-images.json                # Single source of truth: image -> apt_file/req_file/runtime_apt_file/final_target (no version)
│
├── apt.sandbox.base.txt               # Builder-stage OS packages for the base-apt family
├── apt.sandbox.extended.txt           # Builder-stage OS packages for the extended-apt family
├── apt.sandbox.base.runtime.txt       # Documented fallback only — not consumed by any tag today (see hardening doc)
├── apt.sandbox.extended.runtime.txt   # final-full runtime packages for -data only
├── apt.sandbox.malware.runtime.txt    # final-full runtime packages for -malware (extended.runtime + DFIR libs: Sleuth Kit/libewf/AFF/VHDI/VMDK)
├── apt.sandbox.network.runtime.txt    # final-full runtime packages for -network (extended.runtime + tshark/nmap/libkrb5)
├── apt.sandbox.cti.runtime.txt        # final-full runtime packages for -cti (libmagic1t64 — pycti transitively imports python-magic; libbrotli1 — Pillow's FreeType)
├── apt.sandbox.rules.runtime.txt      # final-full runtime packages for -rules (git — GitPython shells out to it at import time)
├── apt.sandbox.dev.txt                # Builder-stage OS packages of python-dev (compilers and headers for the lock's source wheels)
├── apt.sandbox.toolchain.runtime.txt  # Final-stage packages of every toolchain target (git, ca-certificates, build-essential, pkg-config, openssh-client, libmagic1t64: the named exception of the hardening doc's "Toolchain images")
├── apt.sandbox.dev.runtime.txt        # Runtime packages of python-dev and nodejs-dev (git — the one runtime CLI exception, named; shared libraries of the locked backend); python-dev also bakes Playwright's headless Chromium under /ms-playwright for the lab's presentation export
│
├── requirements.sandbox.base.txt      # Base data-science packages (pinned)
├── requirements.sandbox.extended.txt  # Full catalog — NOT installable as one env
│
├── requirements.sandbox.cti.txt       # CTI, IOC enrichment, SIEM
├── requirements.sandbox.rules.txt     # Rules engineering (Sigma, pySigma, …)
├── requirements.sandbox.network.txt   # Network automation, PCAP, protocols, identity
├── requirements.sandbox.malware.txt   # Malware RE + DFIR
├── requirements.sandbox.soar.txt      # SOAR, cloud security, supply chain, email
└── requirements.sandbox.data.txt      # Data science, ML, NLP, visualization, docs
```

---

## Dockerfile Design

### Multi-stage: builder, final-minimal, final-full

`Dockerfile.sandbox.python` has five stages, three of them images (the
`busybox` and `python-runtime` stages only feed `final-minimal`; see
[`dev-docs/sandbox-image-hardening.md`](../../dev-docs/sandbox-image-hardening.md)
for the full rationale):

- **`builder`** (Debian-slim) installs the full `$APT_FILE` list (including
  compilers/`*-dev` headers) and installs `$REQ_FILE` into `/opt/venv` with uv
  (`uv venv --seed`, then `uv pip install`).
  Never shipped — only specific files are `COPY --from=builder`'d out.
- **`final-minimal`** (`gcr.io/distroless/cc-debian13`, no package manager,
  non-root UID 65532 by default, plus the builder's own Python 3.14 copied
  from `python:3.14-slim`) — used only by `sandbox-python`
  (base) and `sandbox-python-soar`, the two tags with no runtime
  shared-library/CLI need at all. Ships a pruned static busybox `sh` (copied
  from upstream `busybox:1-musl`, which has no library dependencies) so OpenSandbox's
  injected `bootstrap.sh` has an interpreter to exec and its coreutils
  calls (`tail`, `dirname`, `mkdir`, ...) resolve — without a shell at all
  the container fails to start with `exec /opt/opensandbox/bootstrap.sh: no
  such file or directory`. No package manager or other tool is added, and
  there is no `ENTRYPOINT` (OpenSandbox sets its own; run Python directly with
  `--entrypoint python3`).
- **`final-full`** (Debian-slim, non-root `sandboxuser` UID 1000) — used by
  every other tag: `-data`/`-malware`/`-network` (real runtime shared-library
  needs — GDAL, GEOS, libmagic, libpcap, …) and also `-cti`/`-rules`, which
  were moved here after testing found transitive runtime-CLI needs
  (`libmagic` via `pycti`, `git` via `GitPython`) that `final-minimal` has no
  package manager to satisfy — see `dev-docs/sandbox-image-hardening.md` rule 2.

The final stage is selected with `--target final-minimal` or
`--target final-full`, not a build-arg — see `sandbox-images.json` for which
target each tag uses.

### Parameterised apt and pip files

```dockerfile
ARG APT_FILE=apt.sandbox.base.txt
ARG REQ_FILE=requirements.sandbox.base.txt
ARG RUNTIME_APT_FILE=apt.sandbox.extended.runtime.txt
```

`APT_FILE`/`REQ_FILE` are consumed by `builder`; `RUNTIME_APT_FILE` (the
final-stage-only shared-lib subset) is consumed by `final-full` only —
`final-minimal` has no apt to install it with.

### Comment-safe apt install

```bash
grep -v '^\s*#' /tmp/apt-packages.txt | grep -v '^\s*$' \
  | xargs apt-get install -y --no-install-recommends
```

`xargs` passes each non-blank, non-comment line as a package name. Every
`apt.sandbox.*.txt` and `apt.sandbox.*.runtime.txt` file uses `#` comments for
human readability; the filter makes this safe.

### Pillow and the system Brotli

The Pillow wheels bundle Brotli 1.2.0 (CVE-2026-111497) for FreeType.
`builder` replaces the bundled `libbrotlidec` with a link to the system
`libbrotlidec.so.1` and removes the bundled `libbrotlicommon`. `final-minimal`
gets the system library from `python-runtime`, `final-full` from `libbrotli1`
in the tag's `*.runtime.txt`. Remove the step once a Pillow release bundles
Brotli 1.2.1 or later.

### Build examples

```bash
# Base (default, final-minimal)
docker build -f backend/sandbox/Dockerfile.sandbox.python \
  --target final-minimal -t sandbox-python:<version> .

# CTI domain (final-full — pycti transitively needs libmagic at runtime)
docker build -f backend/sandbox/Dockerfile.sandbox.python \
  --target final-full \
  --build-arg APT_FILE=apt.sandbox.base.txt \
  --build-arg REQ_FILE=requirements.sandbox.cti.txt \
  --build-arg RUNTIME_APT_FILE=apt.sandbox.cti.runtime.txt \
  -t sandbox-python-cti:<version> .

# Data science domain (final-full — needs GDAL/etc. system libs)
docker build -f backend/sandbox/Dockerfile.sandbox.python \
  --target final-full \
  --build-arg APT_FILE=apt.sandbox.extended.txt \
  --build-arg REQ_FILE=requirements.sandbox.data.txt \
  --build-arg RUNTIME_APT_FILE=apt.sandbox.extended.runtime.txt \
  -t sandbox-python-data:<version> .
```

Or use `./build-image.sh <image>:<tag>`, which looks up the correct target/args
for `<image>` in `sandbox-images.json` (the tag is whatever you pass, normally
the version `OPENSANDBOX_IMAGES` names) — the same script `create-register-images.sh`
and the CI/scan workflows call, so a new build-arg only needs to be added in
one place. See
[`specs/005-harden-sandbox-images/quickstart.md`](../../specs/005-harden-sandbox-images/quickstart.md).

---

## Base Image

**`apt.sandbox.base.txt`** — `gcc`, `libfreetype6`, `libpng16-16` (build deps
for matplotlib/numpy), `libbrotli1` (Pillow's FreeType, see
[Pillow and the system Brotli](#pillow-and-the-system-brotli)).

**`requirements.sandbox.base.txt`** — pinned to the same versions as the
backend's own `pyproject.toml` for consistency:

| Package | Version |
|---------|---------|
| numpy | 2.4.6 |
| pandas | 3.0.3 |
| matplotlib | 3.10.9 |
| scipy | 1.17.1 |
| tabulate | 0.10.0 |
| PyYAML | 6.0.3 |
| openpyxl | 3.1.5 |
| XlsxWriter | 3.2.9 |
| pyzipper | 0.4.0 |
| pycryptodomex | 3.24.2 |
| cryptography | 50.0.0 |
| av (PyAV) | 19.0.2 |
| pypdf | 6.21.4 |
| pypdfium2 | 5.13.0 |

`pyzipper`, `pycryptodomex` and `cryptography` give `code_interpreter` a
password-protected archive without a 7z binary or a subprocess (the image is
distroless): `pyzipper.AESZipFile(path, "w", compression=pyzipper.ZIP_DEFLATED,
encryption=pyzipper.WZ_AES)` then `setpassword(b"...")` writes a WinZip AES-256
(AE-2) ZIP that 7-Zip, libarchive (`bsdtar --passphrase`, Windows `tar`) and
macOS Archive Utility open with the password and refuse without it. CI checks
the round trip on every build of `sandbox-python` (`sandbox-images.yml`,
"AES ZIP round trip"). The password is an argument of the agent's own code:
nothing stores or logs it.

`PyYAML` lets `code_interpreter` parse a YAML file it reads or edits (an
Ansible playbook, a Helm values file, a workflow) with `yaml.safe_load`, to
check an edit before it is pushed; it carries the backend's own pin.

`openpyxl` and `XlsxWriter` let `code_interpreter` read an `.xlsx` a user
attached and write one as a deliverable, directly or through
`pandas.read_excel` and `DataFrame.to_excel`; `openpyxl` carries the backend's
own pin.

`av` (PyAV, BSD-3-Clause) gives `code_interpreter` a video decoder in process:
its manylinux wheel bundles the FFmpeg libraries, so a frame of a recording, an
AI shot or a rendered MP4 is read with no `ffmpeg` binary and no subprocess.
The frame at a moment, as a PNG:

```python
import av
with av.open("attachments/render.mp4") as video:
    stream = video.streams.video[0]
    video.seek(int(2.5 / stream.time_base), stream=stream, backward=True)
    frame = next(f for f in video.decode(stream) if f.time >= 2.5)
    frame.to_image().save("frame-2.5s.png")
```

CI encodes a short MP4 inside the image with no network and reads the frame at
1 s back on every build of `sandbox-python` (`sandbox-images.yml`, "Video frame
round trip"), the h264, hevc, vp9 and mpeg4 decoders checked present.

`pypdf` (BSD-3-Clause, pure Python) and `pypdfium2` (BSD-3-Clause and
Apache-2.0, its wheel bundles PDFium: no system library) let `code_interpreter`
read a PDF a user attached: the AcroForm fields, the signature fields (`/Sig`:
signer name, signing time, `/ByteRange`, `/Contents`), the text and the images
with `pypdf`, and a page rendered to an image with `pypdfium2`. Both carry the
backend's own pins; PyMuPDF is left out on purpose (AGPL).

```python
import pypdf, pypdfium2
reader = pypdf.PdfReader("attachments/contract.pdf")
for name, field in (reader.get_fields() or {}).items():
    if field.get("/FT") == "/Sig":
        sig = field.get("/V")  # None while the field is unsigned
        print(name, sig and sig.get("/Name"), sig and sig.get("/M"), sig and sig.get("/ByteRange"))
pypdfium2.PdfDocument("attachments/contract.pdf")[0].render(scale=2).to_pil().save("page-1.png")
```

CI writes a 2-page PDF with one text field and one empty signature field inside
the image with no network, reads both fields back with their type and renders
page 1 to a PNG on every build of `sandbox-python` (`sandbox-images.yml`, "PDF
round trip").

---

## Why the Extended File Cannot Be Installed as One Environment

`requirements.sandbox.extended.txt` is a **reference catalog** of ~200 packages
across 20 tool families. It cannot be installed in a single Python environment
because several packages have mutually incompatible version constraints on shared
transitive dependencies:

| Conflict | Package A | Requires | Package B | Requires |
|----------|-----------|----------|-----------|----------|
| `click` | `greynoise 3.0.3` | `click<=8.1.8` | `sigma-cli 3.0.3` | `click>=8.3.1` |
| `click` | `mitreattack-python 6.1.0` | `click>=8.1.8` | `greynoise 3.0.3` | `click<=8.1.8` |
| `pandas` | `mitreattack-python` | `pandas<2.x` range | `msticpy 3.0.1` | upper-bound constraints |
| `requests` | `pymisp` | upper-bound | latest `requests` | — |

---

## Per-Domain Files

Each file installs cleanly as an independent environment.
Infrastructure packages (`requests`, `click`, `packaging`, etc.) are listed
**unpinned** at the bottom of each file so pip's resolver can satisfy all
domain packages' transitive constraints.

| File | apt profile | Domains |
|------|-------------|---------|
| `requirements.sandbox.cti.txt` | base | CTI platforms (MISP, OpenCTI, STIX, TAXII, ATT&CK, VirusTotal, OTX, Shodan, Censys, GreyNoise), IOC enrichment, SIEM/hunting (msticpy, Kqlmagic, Splunk, Elasticsearch), threat hunting |
| `requirements.sandbox.rules.txt` | base | Detection rules (pySigma, sigma-cli), YAML/JSON schema (ruamel.yaml, yamllint, jsonschema, pydantic, Jinja2), testing (pytest, pre-commit), Git |
| `requirements.sandbox.network.txt` | extended | Network automation (netmiko, napalm, scrapli, nornir, Cisco, PAN-OS, FortiGate), PCAP/protocols (scapy, pyshark, dpkt, dnslib, impacket, nmap), identity/AD (ldap3, impacket, msldap, pyspnego, Kerberos, NTLM, neo4j) |
| `requirements.sandbox.malware.txt` | extended | Malware RE (yara-python, plyara, pefile, lief, oletools, python-magic, tlsh, capstone, keystone-engine, unicorn), DFIR (volatility3, pytsk3, python-registry, dfvfs, plaso, timesketch) |
| `requirements.sandbox.soar.txt` | base | SOAR (TheHive, Cortex, Jira, GitLab, GitHub, Slack, Teams, ServiceNow), cloud security (AWS, Azure, GCP, Kubernetes, Docker), supply chain (CycloneDX, SPDX, pip-audit, safety, bandit, sigstore), email security (DKIM, DMARC, SPF, IMAP, Exchange), automation (Celery, FastAPI, loguru) |
| `requirements.sandbox.data.txt` | extended | Data science (numpy, pandas, scipy, pyarrow), ML (scikit-learn, tensorflow, keras, xgboost), NLP (nltk, spacy, textblob), statistics (networkx, statsmodels, sympy, ortools), performance (numba, llvmlite), visualization (matplotlib, seaborn, plotly, bokeh), image (opencv, pillow, scikit-image), documents (openpyxl, pypdf, python-docx), geospatial (geopandas, pyproj, shapely) |

### CTI vs Rules split

`cti.txt` and `rules.txt` were originally one file. They were separated because
`greynoise` (in CTI) pins `click<=8.1.8` while `sigma-cli` (in Rules) requires
`click>=8.3.1` — there is no version of click that satisfies both.

---

## Known Package Incompatibilities

### `tensorflow==2.21.0` in `requirements.sandbox.data.txt` — no Python 3.14 wheel

Re-verified 2026-08-10: `pip install tensorflow==2.21.0` fails outright
against the current `python:3.14-slim` base — `ERROR: No matching
distribution found for tensorflow==2.21.0` (no wheel published for this
Python version at all, not just this exact pin). This means
`sandbox-python-data` currently fails to build **even on an unmodified
Dockerfile** — confirmed by building the pre-hardening single-stage image
too. This is a pre-existing dependency-pinning issue, unrelated to and out of
scope for the sandbox-image-hardening work; it needs either a
newer `tensorflow` release with 3.14 wheels or a Python downgrade for this
one domain image, tracked separately.

### `ssdeep 3.4` — removed from all files

`ssdeep 3.4` has no wheel for Python 3.13 or 3.14 (re-verified 2026-08-10
against the current `python:3.14-slim` base — same failure). Building from
source fails because its `pyproject.toml` build backend
(`setuptools.build_meta`) runs a `setup.py` that imports `pkg_resources`,
which is no longer exposed in pip's isolated build environments on modern
pip / setuptools 82+, regardless of Python version.

**Replacement:** `tlsh` (TLSH fuzzy hashing) is already included in
`requirements.sandbox.malware.txt` and covers the similarity-hashing use case.

### `PyPDF2 3.0.1` - replaced by `pypdf`

`PyPDF2` is abandoned: its maintainers merged it back into `pypdf`, and 3.0.1
is its final release. It carries GHSA-4vvm-4w3v-6mr8 (CVE-2023-36464, a
crafted PDF sends content-stream parsing into an infinite loop), which will
never be fixed in `PyPDF2`; `pypdf` fixed it in 3.9.0. `requirements.sandbox.data.txt`
and `requirements.sandbox.extended.txt` therefore list `pypdf`, pinned to the
same version as `backend/pyproject.toml` so the sandbox and the backend do
not drift.

**Migrating a custom tool:** `import PyPDF2` fails in an image built from the
current files. Rename the import, and replace `PdfMerger` (removed in
`pypdf` 5.0) with `PdfWriter`:

```python
# Before (PyPDF2)
from PyPDF2 import PdfReader, PdfMerger

merger = PdfMerger()
for path in paths:
    merger.append(path)
merger.write("merged.pdf")

# After (pypdf)
from pypdf import PdfReader, PdfWriter

writer = PdfWriter()
for path in paths:
    writer.append(path)
writer.write("merged.pdf")
```

`PdfReader`, `PdfWriter`, `.pages` and `.extract_text()` keep the same
names. Upstream references: merging with `PdfWriter`
(<https://pypdf.readthedocs.io/en/stable/user/merging-pdfs.html>) and the
changelog of removed names
(<https://pypdf.readthedocs.io/en/stable/meta/CHANGELOG.html>).

### `thinc 9.1.1` and spaCy Cython internals — not pinned separately

`thinc` (and its sub-packages `cymem`, `murmurhash`, `preshed`, `srsly`,
`wasabi`, `weasel`, `catalogue`, `confection`) are **spaCy internal
dependencies**. Pinning them independently causes a Cython compilation
failure (`thinc/backends/numpy_ops.pyx: Python objects cannot be cast to
pointers of primitive types`) because `thinc 9.1.1` uses the old `cdef
ndarray` Cython type that was removed in numpy 2.x. This is a numpy/Cython
ABI mismatch, not a Python-interpreter-version issue — re-verified 2026-08-10
against Python 3.14 (the current base image), same failure as originally
found on 3.13.

**Resolution:** Only `spacy==3.8.14` is listed. pip pulls the correct
compatible versions of all Cython sub-packages automatically.

### `h5py` — unpinned in `requirements.sandbox.data.txt`

`tensorflow 2.21.0` requires `h5py>=3.11.0,<3.15.0`. Pinning `h5py==3.16.0`
(latest at the time) violated this upper bound.

### `mpmath` — unpinned in `requirements.sandbox.data.txt`

`sympy 1.14.0` requires `mpmath>=1.1.0,<1.4`. Pinning `mpmath==1.4.1`
(latest) violated this upper bound.

---

## Versions

All version pins in the domain files were resolved live from PyPI at the time of
authoring (2026-07-02) using the PyPI JSON API. Transitive infrastructure
packages are left unpinned so pip's resolver can satisfy domain packages'
upper-bound constraints without manual intervention.

## Publishing the custom-tools images

`sandbox-python` and `sandbox-nodejs` are published by XTM-One-Platform/release
(`build-push-sandbox.yml`, `images=custom-tools`) as `xtmone/sandbox-python` and
`xtmone/sandbox-nodejs` under the version of `published-images.json`, which also
holds the SHA-256 of each image's build inputs (its Dockerfile, the apt and
requirement files of its `sandbox-images.json` entry, the entry itself).
`check-published-images.py` runs on every pull request (`Sandbox Image Pin`) and
fails when those inputs changed without a new version, or when
`_OPENSANDBOX_DEFAULT_IMAGES` (`backend/app/core/config.py`) names another
version. To change either image:

1. change its inputs, bump `version` in `published-images.json`, write the
   hashes `check-published-images.py --print-hashes` prints, and move the app
   default (and its test) to the same version;
2. merge: the push to main dispatches the build of that version (the release
   App of the `deploy-trigger` environment), skipped when the tag exists;
3. move the pinned Pool images of the deployments (`opensandbox.pools`) to it.

The toolchain images (`xtmone/sandbox-toolchain-python`, `-node`, `-jvm`,
`-rust`, `-dbt`: the stages of `Dockerfile.sandbox.toolchain`) are a second
family of the same manifest, under its `toolchain` object: one `version` for
the five images (`build-push-sandbox.yml`, `images=toolchain`) and the SHA-256
of their build inputs (`Dockerfile.sandbox.toolchain`,
`apt.sandbox.toolchain.runtime.txt`, `apt.sandbox.toolchain.rust.txt`). The
same check holds them: a change to those files needs a new `toolchain.version`
and its hash, the merge dispatches the build of that version, then the
`toolchain-*` Pools of the deployments move to it. The app default does not
name the toolchain images; the deployments pin them.

The dev images of `sandbox_run_command` (`xtmone/sandbox-python-dev` and
`xtmone/sandbox-nodejs-dev`: the stages of `Dockerfile.sandbox.dev`) are the
third family, under the `dev` object: one `version` for both
(`build-push-sandbox.yml`, `images=dev`) and the SHA-256 of their build inputs
(`Dockerfile.sandbox.dev`, `apt.sandbox.dev.txt`, `apt.sandbox.dev.runtime.txt`
and `.xtm-one/stores.py`, whose pins python-dev bakes under `/opt/stores`). A
change to them needs a new `dev.version` and its hash; the merge dispatches the
build, then the `python-dev` and `nodejs-dev` Pools of the deployments move to
it. The locks the images install (`backend/uv.lock`, `frontend/yarn.lock`) are
not inputs: a dependency bump does not need a new image. python-dev also
carries tiktoken's `cl100k_base` encoding under `/opt/tiktoken`
(`TIKTOKEN_CACHE_DIR`), fetched at build time, so the unit tests that count
tokens run with no network. The
`backend-integration` check needs the `postgres`, `redis` and `s3` artefacts of
`stores.py` at their pins (`stores.CHECK_ARTEFACTS`); the `postgres-contrib`
archive the product labs need is optional for it, so an image baked before a
new lab-only artefact still runs the integration suite.
