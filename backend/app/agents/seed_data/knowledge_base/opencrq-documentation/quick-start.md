# 🚀 Quick Start Guide

The easiest way to discover OpenCRQ is using **Docker**. This method sets up the full stack (Backend, Frontend, and Database) in minutes.

## Prerequisites
- [Docker](https://docs.docker.com/get-docker/) or [Podman](https://podman.io/)

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/OpenCRQ-Platform/opencrq.git
    cd opencrq
    ```

2.  **Configure XTM One:**
    ```bash
    cd docker
    cp .env.sample .env
    ```
    Set `XTM_ONE__URL`, `XTM_ONE__TOKEN` and `XTM_ONE__PLATFORM_URL` in `.env`: the platform refuses to start without them. See [XTM One](deployment/configuration.md#xtm-one-ai-assistant).

3.  **Start the application:**
    ```bash
    docker compose up --build --pull always -d
    ```
    *(You can also use `podman compose up --build --pull=always -d`)*

4.  **Access the platform:**
    Open your browser and navigate to `http://localhost:3020` (default port).

For detailed configuration options (ports, secrets, database), please refer to the [Docker Deployment Guide](deployment/docker.md).
