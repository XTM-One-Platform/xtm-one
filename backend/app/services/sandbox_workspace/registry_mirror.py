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

``SANDBOX_REGISTRY_MIRROR_URL`` names a read-only mirror proxy of npm, PyPI and
Maven Central (``backend/sandbox/registry-mirror/nginx.conf`` is the shipped one:
GET and HEAD only, ``/npm/``, ``/pypi/`` and ``/maven/`` routes, no credential
forwarded). Set, the
containers ``sandbox_run_command`` and ``sandbox_start_service`` create
(never the code interpreter or a custom tool) get:

- the mirror host in their egress allow-list, on top of
  ``OPENSANDBOX_NETWORK_ALLOW``, and nothing else: the public registries stay
  out of the list, so an install reaches them only through the mirror;
- the registry variables of npm, Yarn (classic and Berry), corepack, pip,
  uv and Poetry (``POETRY_PYPI_MIRROR_URL``, read by the
  poetry-plugin-pypi-mirror the toolchain-python image installs) pointing at
  the mirror, with the offline switches the dev images bake
  (``PIP_NO_INDEX``, ``UV_OFFLINE``, ``YARN_ENABLE_NETWORK``) turned off;
- Maven pointed at the mirror: :func:`write_maven_settings` writes a settings
  file with one mirror of every repository (``mirrorOf *``) to ``/maven/``,
  and ``MAVEN_ARGS`` names it as the global settings. It replaces the
  install's own global settings, whose only entry is the plain-HTTP blocker
  that would refuse an in-cluster ``http://`` mirror.

Unset (the default), nothing changes: the dev images stay offline and install
from the caches they bake. ``Settings`` validates the URL (http or https, a
host, no credential, no query): the mirror is read-only and anonymous.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from app.core.config import settings

__all__ = ["MAVEN_SETTINGS_PATH", "command_egress", "command_env", "maven_settings_xml", "write_maven_settings"]

#: Where the Maven settings of the mirror are written in a command or service container.
MAVEN_SETTINGS_PATH = "/tmp/xtm-registry-mirror-maven-settings.xml"  # noqa: S108


def _url() -> str:
    return (settings.sandbox_registry_mirror_url or "").rstrip("/")


def command_egress() -> frozenset[str]:
    """The egress allow-list of a command or service container: ``OPENSANDBOX_NETWORK_ALLOW`` plus the mirror host."""
    allow = settings.opensandbox_network_allow_list
    host = urlsplit(_url()).hostname if _url() else None
    return allow | {host} if host else allow


def command_env() -> dict[str, str]:
    """The registry variables of npm, Yarn, corepack, pip, uv and Poetry for the mirror; ``{}`` when none is set."""
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
        # Poetry reads neither of the above: poetry-plugin-pypi-mirror (toolchain-python) swaps pypi.org for it.
        "POETRY_PYPI_MIRROR_URL": pypi,
        # Maven (3.9 reads MAVEN_ARGS): the settings write_maven_settings puts in the container.
        "MAVEN_ARGS": f"--global-settings {MAVEN_SETTINGS_PATH}",
    }
    if urlsplit(url).scheme == "http":
        # An in-cluster mirror served over plain HTTP: each client refuses it unless told.
        host = urlsplit(url).hostname or ""
        env["YARN_UNSAFE_HTTP_WHITELIST"] = host
        env["PIP_TRUSTED_HOST"] = host
        env["UV_INSECURE_HOST"] = host
    return env


def maven_settings_xml() -> str:
    """The Maven settings that send every repository to the mirror's ``/maven/`` route; ``""`` when none is set."""
    url = _url()
    if not url:
        return ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0">\n'
        "  <mirrors>\n"
        "    <mirror>\n"
        "      <id>xtm-registry-mirror</id>\n"
        "      <name>Sandbox registry mirror (read-only)</name>\n"
        f"      <url>{escape(url)}/maven/</url>\n"
        "      <mirrorOf>*</mirrorOf>\n"
        "    </mirror>\n"
        "  </mirrors>\n"
        "</settings>\n"
    )


async def write_maven_settings(sandbox: Any) -> None:
    """Write :func:`maven_settings_xml` at ``MAVEN_SETTINGS_PATH`` in *sandbox*; nothing when no mirror is set."""
    xml = maven_settings_xml()
    if xml:
        await sandbox.files.write_file(MAVEN_SETTINGS_PATH, xml.encode("utf-8"))
