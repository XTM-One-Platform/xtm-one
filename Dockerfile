# =============================================================================
# XTM One Platform — unified image (backend API + frontend static files)
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Build frontend
# ---------------------------------------------------------------------------
FROM docker.io/library/node:24.21.0-alpine3.24@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS frontend-build

WORKDIR /app
COPY frontend/package.json frontend/yarn.lock frontend/.yarnrc.yml ./
RUN corepack enable && yarn install --immutable
COPY frontend/ .

# Declared after the dependency install: every RUN that follows an ARG whose
# value changed misses the build cache, and both change with every commit.
ARG VITE_GIT_SHA
ARG APP_VERSION=0.0.0-dev
# Vite only exposes VITE_* env vars to browser code. APP_VERSION remains the
# single external build arg; this maps it into Vite's client-side namespace.
ENV VITE_APP_VERSION=$APP_VERSION
# The bundle only: `yarn build` typechecks first (`tsc -b`, 60-105 s
# on CI for a 5-10 s bundle). The same check, `tsc --noEmit` on the same
# tsconfig, is ci.yml's TypeScript job on every pull request, and Deploy's
# frontend-check gates every image it publishes. A source build
# (`docker compose build`) no longer typechecks: run `yarn build` for that.
RUN yarn vite build

# ---------------------------------------------------------------------------
# Stage 1b: Build Office Add-in
# ---------------------------------------------------------------------------
FROM docker.io/library/node:24.21.0-alpine3.24@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS addin-build

WORKDIR /app
COPY apps/microsoft-office/package.json apps/microsoft-office/package-lock.json* ./
RUN npm ci
COPY apps/microsoft-office/ .
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 1c: Backend dependency list
# ---------------------------------------------------------------------------
# The locked backend dependencies (backend/uv.lock), without the dev group, as
# a hash-pinned requirements list. A stage of its own so the install below is
# keyed on that list alone: pyproject.toml also holds the ruff and pytest
# settings, and editing them must not reinstall every package and re-download
# Chromium. --locked fails the build when pyproject.toml was changed without
# relocking.
FROM docker.io/library/python:3.14-slim AS backend-deps
COPY --from=ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 /uv /usr/local/bin/
WORKDIR /src
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file /requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: Platform image
# ---------------------------------------------------------------------------
FROM docker.io/library/python:3.14-slim

WORKDIR /app

# System dependencies (libxmlsec1 for SAML, git for VCS installs, ffmpeg for
# the Video Studio's recordings and renders, tesseract-ocr for the
# labels a recording reads on a graph canvas, libmagic for
# python-magic, which pycti imports)
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    build-essential libpq-dev git \
    pkg-config libxml2-dev libxmlsec1-dev libxmlsec1-openssl \
    ffmpeg tesseract-ocr \
    libmagic1t64 \
    && rm -rf /var/lib/apt/lists/*

# Node.js 24 (required for stdio MCP servers that use npx). The npm bundled
# with the NodeSource package is used as-is — an unpinned `npm@latest`
# upgrade made the layer non-reproducible. The package is left unpinned so a
# build takes the latest Node 24 security release (nothing bumps an apt pin),
# and fails below 24.22.1, the first release with the CA bundle that blocks the
# compromised eMudhra intermediates; 24.18.1 cleared the CVEs.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL https://deb.nodesource.com/setup_24.x -o nodesource_setup.sh \
    && bash nodesource_setup.sh \
    && apt-get install -y --no-install-recommends nodejs \
    && node_version="$(dpkg-query -W -f='${Version}' nodejs)" \
    && { dpkg --compare-versions "$node_version" ge 24.22.1 \
        || { echo "nodejs $node_version is older than 24.22.1" >&2; exit 1; }; } \
    && rm -f nodesource_setup.sh \
    && rm -rf /var/lib/apt/lists/*

ENV NPM_CONFIG_UPDATE_NOTIFIER=false

# uv/uvx (required for stdio MCP servers distributed as Python packages).
# Copying the prebuilt binaries from astral's official image is faster and
# leaner than running their installer script or pulling in pip/pipx.
COPY --from=ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 /uv /uvx /usr/local/bin/

# The backend-deps list, installed into the image's own Python so `uvicorn`,
# `saq`, `playwright` and `python` resolve as they always have. Every package is
# pinned and hash-checked; --compile-bytecode does what pip did by default, so
# the first import does not pay for it.
COPY --from=backend-deps /requirements.txt /tmp/backend-requirements.txt
# pypi.org can stop answering for several minutes, and a docker build does not
# see the workflow env of CI and Deploy: uv gets the same values here (each
# download retries 5 times at 30 s, the install runs up to 3 times, a minute
# apart). Per command rather than ENV, so the runtime image does not inherit them.
#
# The Pillow wheels bundle Brotli 1.2.0 (CVE-2026-111497) for FreeType: in this
# same layer, replace the bundled libbrotlidec with a link to the system one
# (libbrotli1, fixed) and remove the bundled libbrotlicommon, which only it
# loads.
# The link keeps the bundled name (libbrotlidec-<hash>.so.1.2.0), the one
# FreeType loads, but resolves to the system version.
RUN for attempt in 1 2 3; do \
        UV_HTTP_TIMEOUT=30 UV_HTTP_RETRIES=5 \
            uv pip install --system --require-hashes --no-cache --compile-bytecode \
            -r /tmp/backend-requirements.txt && break; \
        echo "uv pip install failed (attempt $attempt of 3)" >&2; \
        if [ "$attempt" -lt 3 ]; then sleep 60; else exit 1; fi; \
    done \
    && cd /usr/local/lib/python3*/site-packages/pillow.libs \
    && rm libbrotlicommon-* \
    && ln -sf "/usr/lib/$(uname -m)-linux-gnu/libbrotlidec.so.1" libbrotlidec-*

# pycti loads python-magic, which needs libmagic as soon as it is imported,
# and the STIX tools derive standard ids through pycti: a missing
# system library fails the build here, never an agent at runtime.
RUN python -c "import pycti"

# Chromium for the Playwright-backed PDF renderer (generate_file) and the
# browser automation tool. The Python package alone is NOT enough — the browser
# binary and its OS-level libraries must be present or chromium.launch()
# fails at runtime with "Executable doesn't exist". --with-deps pulls the
# required Debian libs; clean apt lists afterwards to keep the image lean.
#
# Install browsers into a fixed, world-readable path instead of root's
# ~/.cache/ms-playwright. In production the container runs as a non-root user
# (Kubernetes runAsUser), whose $HOME is not /root, so a default-location
# install would be invisible at runtime ("Executable doesn't exist at
# /.cache/ms-playwright/..."). Baking PLAYWRIGHT_BROWSERS_PATH as an ENV makes
# the build-time install and the runtime lookup agree for any user.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# A Video Studio recording lane on a GPU node (VIDEO_STUDIO_RECORDING_GL=gpu):
# Chromium draws WebGL through ANGLE on the host's Vulkan or EGL
# driver. The NVIDIA container runtime mounts the driver's libraries
# (libGLX/libEGL_nvidia, the Vulkan ICD) only with the graphics capability, and
# the Vulkan loader and the EGL vendor dispatch must be in the image. Without a
# GPU nothing here is used: Chromium draws on SwiftShader as before.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libvulkan1 libegl1 libgles2 \
    && rm -rf /var/lib/apt/lists/*
# The GLVND EGL vendor file and the Vulkan ICD file that point the dispatch at
# the NVIDIA libraries the runtime mounts. Without them the EGL
# dispatch and the Vulkan loader only see Mesa, and Chromium's ANGLE draws on
# llvmpipe (a CPU rasterizer) on a GPU node. On a node without the NVIDIA
# driver the libraries are absent: the loaders skip these files and fall back
# as before.
RUN mkdir -p /usr/share/glvnd/egl_vendor.d /usr/share/vulkan/icd.d \
    && printf '{"file_format_version": "1.0.0", "ICD": {"library_path": "libEGL_nvidia.so.0"}}\n' \
       > /usr/share/glvnd/egl_vendor.d/10_nvidia.json \
    && printf '{"file_format_version": "1.0.0", "ICD": {"library_path": "libGLX_nvidia.so.0", "api_version": "1.3"}}\n' \
       > /usr/share/vulkan/icd.d/nvidia_icd.json
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics

# Chromium reads its certificate trust settings from $HOME/.pki/nssdb, and only
# when it can open it read-write: the roots in docker/chromium-distrusted-roots
# are marked as not trusted there, for root (HOME=/root) and for a Kubernetes
# runAsUser with no passwd entry (HOME=/, group 0).
COPY docker/chromium-distrusted-roots/ /tmp/chromium-distrusted-roots/
RUN apt-get update && apt-get install -y --no-install-recommends libnss3-tools \
    && mkdir -p /etc/chromium/nssdb /root/.pki /.pki \
    && certutil -d sql:/etc/chromium/nssdb -N --empty-password \
    && for cert in /tmp/chromium-distrusted-roots/*.pem; do \
         certutil -d sql:/etc/chromium/nssdb -A -t p,p,p -n "$(basename "$cert" .pem)" -i "$cert"; \
       done \
    && chmod -R a+rX,g+w /etc/chromium/nssdb \
    && ln -s /etc/chromium/nssdb /root/.pki/nssdb \
    && ln -s /etc/chromium/nssdb /.pki/nssdb \
    && apt-get purge -y libnss3-tools \
    && rm -rf /var/lib/apt/lists/* /tmp/chromium-distrusted-roots

# Backend application
COPY backend/ .

# The XTM One documentation of this release: the boot seed fills the
# built-in "XTM-One Documentation" knowledge base from it (app/agents/seed.py,
# bundled_docs_dir), so every deployment's assistants read the docs they run.
COPY docs/ /app/xtm_one_docs/

# Built frontend (served by FastAPI catch-all in main.py)
COPY --from=frontend-build /app/dist /app/frontend_dist

# Built Office Add-in (served at /office/ by FastAPI static mount)
COPY --from=addin-build /app/dist /app/office_addin_dist

# Native runner distribution (served at /api/v1/runners/dist/...). The release
# workflow's runner-build job (XTM-One-Platform/release) stages installers,
# signed self-update artifacts and bare-binary archives into runner-dist/
# before this build; a local or source build has only the tracked placeholder
# and the platform reports runner downloads as unavailable instead of failing.
# The headless install scripts ship from source so every image serves them at
# their stable names.
COPY runner-dist/ /app/runner_dist/
COPY apps/runner/dist/install.sh /app/runner_dist/xtm-one-runner-installer.sh
COPY apps/runner/dist/install.ps1 /app/runner_dist/xtm-one-runner-installer.ps1
# The install scripts check the archive's release signature against the public
# keys they carry. Write in the keys and the signature floor this
# release trusts - what CI compiles into the runner, apps/runner/release-signing.json -
# so a served script trusts the build it installs, a fork's own keys included.
# On-premises relay distribution (served at /api/v1/relays/dist/...).
# The release workflow's relay-build job stages the signed archives and their
# VERSION into relay-dist/; a source build has only the placeholder and the
# platform reports relay downloads as unavailable. The relay is signed with
# the runner's release keys, so its install scripts get the same keys.
COPY relay-dist/ /app/relay_dist/
COPY apps/relay/dist/install.sh /app/relay_dist/xtm-one-relay-installer.sh
COPY apps/relay/dist/install.ps1 /app/relay_dist/xtm-one-relay-installer.ps1
COPY apps/runner/release-signing.json apps/runner/dist/embed_release_key.py /tmp/runner-release-key/
RUN python /tmp/runner-release-key/embed_release_key.py /tmp/runner-release-key/release-signing.json \
        /app/runner_dist/xtm-one-runner-installer.sh /app/runner_dist/xtm-one-runner-installer.ps1 \
        /app/relay_dist/xtm-one-relay-installer.sh /app/relay_dist/xtm-one-relay-installer.ps1 \
    && rm -rf /tmp/runner-release-key

# Ensure the shared uvicorn launcher is executable regardless of the host
# filesystem permissions used during ``COPY backend/ .`` (Windows checkouts
# typically lose the +x bit).
RUN chmod +x /app/start-uvicorn.sh

# Version metadata — injected at build time. Last, so a new version reuses
# every cached layer above: a RUN after a changed ARG misses the cache.
ARG APP_VERSION=0.0.0-dev
ENV APP_VERSION=$APP_VERSION
ARG VITE_GIT_SHA=
ENV GIT_SHA=$VITE_GIT_SHA
LABEL org.opencontainers.image.version=$APP_VERSION \
      org.opencontainers.image.revision=$VITE_GIT_SHA \
      org.opencontainers.image.source="https://github.com/XTM-One-Platform/xtm-one" \
      org.opencontainers.image.title="XTM One" \
      org.opencontainers.image.licenses="Apache-2.0 AND LicenseRef-XTM-One-Enterprise-Edition"

EXPOSE 4000

# Numeric tunables (WEB_WORKERS, MAX_CONCURRENCY, WS_PING_INTERVAL,
# WS_PING_TIMEOUT, MAX_REQUESTS, MAX_REQUESTS_JITTER,
# MAX_REQUESTS_DEFER_SECONDS, GRACEFUL_TIMEOUT) and
# uvicorn flag composition live in ``backend/start-uvicorn.sh``. The script
# validates each of those as a non-negative integer and emits a ``WARNING:``
# on stderr for misconfigurations (e.g. ``WS_PING_INTERVAL=30s``) instead of
# silently masking them. FORWARDED_ALLOW_IPS is passed through as is, never
# replaced by a default that trusts a different set of peers; uvicorn keeps an
# entry that is not an IP address or network as a literal no peer matches, and
# the platform names such entries in a startup warning (app/core/proxy_headers.py).
CMD ["/app/start-uvicorn.sh", "4000"]
