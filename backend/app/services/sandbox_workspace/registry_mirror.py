# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The package registries of the command and service containers, through one read-only mirror.

``SANDBOX_REGISTRY_MIRROR_URL`` names a read-only mirror proxy of npm and PyPI
(``backend/sandbox/registry-mirror/nginx.conf`` is the shipped one: GET and
HEAD only, ``/npm/`` and ``/pypi/`` routes, no credential forwarded). Set, the
containers ``sandbox_run_command`` and ``sandbox_start_service`` create
(never the code interpreter or a custom tool) get:

- the mirror host in their egress allow-list, on top of
  ``OPENSANDBOX_NETWORK_ALLOW``, and nothing else: the public registries stay
  out of the list, so an install reaches them only through the mirror;
- the registry variables of npm, Yarn (classic and Berry), corepack, pip and
  uv pointing at the mirror, with the offline switches the dev images bake
  (``PIP_NO_INDEX``, ``UV_OFFLINE``, ``YARN_ENABLE_NETWORK``) turned off.

Unset (the default), nothing changes: the dev images stay offline and install
from the caches they bake. ``Settings`` validates the URL (http or https, a
host, no credential, no query): the mirror is read-only and anonymous.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.core.config import settings

__all__ = ["command_egress", "command_env"]


def _url() -> str:
    return (settings.sandbox_registry_mirror_url or "").rstrip("/")


def command_egress() -> frozenset[str]:
    """The egress allow-list of a command or service container: ``OPENSANDBOX_NETWORK_ALLOW`` plus the mirror host."""
    allow = settings.opensandbox_network_allow_list
    host = urlsplit(_url()).hostname if _url() else None
    return allow | {host} if host else allow


def command_env() -> dict[str, str]:
    """The registry variables of npm, Yarn, corepack, pip and uv for the mirror; ``{}`` when none is set."""
    url = _url()
    if not url:
        return {}
    npm = f"{url}/npm/"
    pypi = f"{url}/pypi/simple/"
    env = {
        # npm, Yarn classic, corepack (the Yarn release a package.json pins).
        "NPM_CONFIG_REGISTRY": npm,
        "YARN_REGISTRY": npm,
        "COREPACK_NPM_REGISTRY": f"{url}/npm",
        # Yarn Berry, and the switch nodejs-dev bakes off.
        "YARN_NPM_REGISTRY_SERVER": f"{url}/npm",
        "YARN_ENABLE_NETWORK": "true",
        "COREPACK_ENABLE_NETWORK": "1",
        # pip and uv, and the switches python-dev bakes on.
        "PIP_INDEX_URL": pypi,
        "PIP_NO_INDEX": "0",
        "UV_DEFAULT_INDEX": pypi,
        "UV_OFFLINE": "false",
    }
    if urlsplit(url).scheme == "http":
        # An in-cluster mirror served over plain HTTP: each client refuses it unless told.
        host = urlsplit(url).hostname or ""
        env["YARN_UNSAFE_HTTP_WHITELIST"] = host
        env["PIP_TRUSTED_HOST"] = host
        env["UV_INSECURE_HOST"] = host
    return env
