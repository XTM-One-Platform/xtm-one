"""The OpenSandbox server of the shipped stack is opt-in, and the dev stack runs it with a key.

OpenSandbox server v0.2.1 refuses to start without an API key. Started with
every ``docker compose up``, the ``opensandbox`` service restarted in a loop on
each deployment that does not use OpenSandbox and sets no
``OPENSANDBOX_API_KEY``. ``docker-compose.yml`` keeps it behind the
``opensandbox`` profile; ``docker-compose.dev.yml`` lifts the profile and gives
the server and its clients (the platform, the workers) the same default key.

The shipped stack forwards the sandbox settings of ``.env`` to the platform
and the workers, and lets them reach the sandboxes the server advertises as
``host.docker.internal``: before, ``CUSTOM_TOOL_SANDBOX=opensandbox``
in ``.env`` never reached a container and the app stayed on ``auto``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

_DEFAULTED = re.compile(r"^\$\{OPENSANDBOX_API_KEY:-(?P<default>[^}]*)\}$")

_APP_SERVICES = ["platform", "worker", "worker-preview"]

# The sandbox settings the shipped stack forwards, with the default it ships:
# the platform's own, except the domain, which names the opensandbox service.
_FORWARDED_SANDBOX_SETTINGS = {
    "CUSTOM_TOOL_SANDBOX": str(Settings.model_fields["custom_tool_sandbox"].default),
    "OPENSANDBOX_DOMAIN": "opensandbox:8090",
    "OPENSANDBOX_PROTOCOL": str(Settings.model_fields["opensandbox_protocol"].default),
    "OPENSANDBOX_API_KEY": str(Settings.model_fields["opensandbox_api_key"].default),
    "OPENSANDBOX_USE_SERVER_PROXY": str(Settings.model_fields["opensandbox_use_server_proxy"].default).lower(),
    "OPENSANDBOX_NETWORK_ALLOW": str(Settings.model_fields["opensandbox_network_allow"].default),
    "SANDBOX_REGISTRY_MIRROR_URL": str(Settings.model_fields["sandbox_registry_mirror_url"].default),
}


@dataclass(frozen=True)
class _Tagged:
    tag: str
    value: Any


def _compose(name: str) -> dict:
    """A compose file of the repository, its ``!reset`` / ``!override`` values kept with their tag."""

    class _Loader(yaml.SafeLoader):
        pass

    def tagged(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        return _Tagged(f"!{suffix}", value)

    _Loader.add_multi_constructor("!", tagged)
    return yaml.load((REPO_ROOT / name).read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506 - SafeLoader with tags kept as values


def _default_key(value: str) -> str:
    match = _DEFAULTED.match(value)
    assert match is not None, value
    return match["default"]


def test_the_shipped_stack_starts_the_opensandbox_server_only_with_its_profile():
    services = _compose("docker-compose.yml")["services"]

    assert services["opensandbox"]["profiles"] == ["opensandbox"]
    # A service of the default profile that depended on it would not start
    # without the profile.
    dependants = [
        name
        for name, service in services.items()
        if name != "opensandbox" and "opensandbox" in (service.get("depends_on") or {})
    ]
    assert dependants == []


def test_the_dev_stack_always_starts_the_opensandbox_server():
    override = _compose("docker-compose.dev.yml")["services"]["opensandbox"]

    assert override["profiles"] == _Tagged("!reset", [])


def test_the_dev_platform_and_workers_send_the_dev_server_its_own_default_key():
    compose = _compose("docker-compose.dev.yml")
    server_key = _default_key(compose["services"]["opensandbox"]["environment"]["OPENSANDBOX_SERVER_API_KEY"])
    client_key = _default_key(compose["x-dev-backend"]["environment"]["OPENSANDBOX_API_KEY"])

    assert server_key != ""
    assert client_key == server_key


@pytest.mark.parametrize("service", _APP_SERVICES)
@pytest.mark.parametrize("name", sorted(_FORWARDED_SANDBOX_SETTINGS))
def test_the_shipped_stack_forwards_the_sandbox_settings_of_env(service, name):
    environment = _compose("docker-compose.yml")["services"][service]["environment"]

    assert environment.get(name) == f"${{{name}:-{_FORWARDED_SANDBOX_SETTINGS[name]}}}"


def test_the_shipped_domain_default_names_the_opensandbox_service():
    services = _compose("docker-compose.yml")["services"]
    host, _, port = _FORWARDED_SANDBOX_SETTINGS["OPENSANDBOX_DOMAIN"].partition(":")

    assert host == services["opensandbox"]["container_name"]
    assert f"localhost:{port}/health" in " ".join(services["opensandbox"]["healthcheck"]["test"])


@pytest.mark.parametrize("service", _APP_SERVICES)
def test_the_shipped_app_services_resolve_the_host_the_sandboxes_are_advertised_on(service):
    services = _compose("docker-compose.yml")["services"]

    assert "host.docker.internal:host-gateway" in services[service]["extra_hosts"]
    assert "host.docker.internal:host-gateway" in services["opensandbox"]["extra_hosts"]


@pytest.mark.parametrize("overlay", ["docker-compose.e2e.yml", "docker-compose.relay-e2e.yml"])
def test_no_service_joining_another_namespace_keeps_a_host_mapping(overlay):
    """Docker refuses ``extra_hosts`` together with ``network_mode`` ("conflicting options")."""
    base = _compose("docker-compose.yml")["services"]
    overrides = _compose(overlay)["services"]

    for name, override in overrides.items():
        if "network_mode" not in override:
            continue
        extra_hosts = override.get("extra_hosts", (base.get(name) or {}).get("extra_hosts"))
        if isinstance(extra_hosts, _Tagged):
            assert extra_hosts.tag == "!reset", name
            extra_hosts = extra_hosts.value
        assert not extra_hosts, name
