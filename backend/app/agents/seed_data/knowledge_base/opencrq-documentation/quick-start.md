# 🚀 Quick Start Guide

OpenCRQ signs its users in through XTM One, so the easiest way to discover it is the
[XTM Docker bundle](https://github.com/FiligranHQ/xtm-docker): one Compose file that runs OpenCRQ
beside XTM One, OpenCTI and OpenAEV, on a shared PostgreSQL and object storage, registered with
XTM One and connected to OpenCTI and OpenAEV. Its [README](https://github.com/FiligranHQ/xtm-docker#readme)
is the reference for the bundle: what follows is the path to a first sign-in to OpenCRQ.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with Docker Compose v2, or [Podman](https://podman.io/)
- The memory and disk the bundle's README asks for: it runs the whole XTM stack, not OpenCRQ alone.

## Installation

1.  **Clone the bundle:**
    ```bash
    git clone https://github.com/FiligranHQ/xtm-docker.git
    cd xtm-docker
    ```

2.  **Configure it:**
    ```bash
    cp .env.sample .env
    ```
    Replace every credential, token and key the sample ships (`ChangeMe`, `changeme` and every
    value marked `[MANDATORY]`), as the README's quick start describes: they are published.
    `XTM_ONE_ADMIN_PASSWORD` protects OpenCRQ's super administrator. Three more matter to OpenCRQ:
    `OPENCRQ_POSTGRES_PASSWORD`, without which OpenCRQ does not start; `OPENCRQ_ADMIN_TOKEN`, a
    UUIDv4 OpenAEV calls OpenCRQ with; and `PLATFORM_REGISTRATION_TOKEN`, with which XTM One
    registers no new platform while it keeps its published default.

3.  **Start the stack:**
    ```bash
    docker compose up -d
    ```
    *(or `podman compose up -d`)*. The first start takes several minutes.

4.  **Sign in:** open `http://localhost:8082` (`OPENCRQ_PORT`) and choose **Sign in with XTM One**,
    with `XTM_ONE_ADMIN_EMAIL` and `XTM_ONE_ADMIN_PASSWORD`. XTM One's administrator is OpenCRQ's
    super administrator, and lands in the tenant OpenCRQ creates at its first start.

5.  **Check the integrations:** that tenant's OpenCTI and OpenAEV connectors are already set, and
    **Integrations** shows them. In OpenCTI, OpenCRQ creates its "OpenCRQ threat feed" live stream at
    start.

To run OpenCRQ against an XTM One that already runs elsewhere, see the
[Docker Deployment Guide](deployment/docker.md).
