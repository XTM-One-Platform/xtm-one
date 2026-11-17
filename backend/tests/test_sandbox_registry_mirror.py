"""The command and service containers reach npm and PyPI through the read-only mirror only (#25)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from app.core.config import Settings, settings
from app.services.sandbox_workspace import registry_mirror

SetMirror = Callable[..., None]

REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_CONF = REPO_ROOT / "backend" / "sandbox" / "registry-mirror" / "nginx.conf"


@pytest.fixture
def mirror(monkeypatch: pytest.MonkeyPatch) -> SetMirror:
    def _set(url: str, allow: str = "") -> None:
        monkeypatch.setattr(settings, "sandbox_registry_mirror_url", url)
        monkeypatch.setattr(settings, "opensandbox_network_allow", allow)

    return _set


def test_without_a_mirror_nothing_changes(mirror: SetMirror) -> None:
    mirror("", allow="api.example.com")

    assert registry_mirror.command_env() == {}
    assert registry_mirror.command_egress() == frozenset({"api.example.com"})


def test_the_mirror_host_alone_joins_the_egress_list(mirror: SetMirror) -> None:
    mirror("https://registry-mirror.sandbox.svc:8443", allow="api.example.com")

    egress = registry_mirror.command_egress()

    assert egress == frozenset({"api.example.com", "registry-mirror.sandbox.svc"})
    assert not egress & {"registry.npmjs.org", "pypi.org", "files.pythonhosted.org", "registry.yarnpkg.com"}


def test_every_package_client_points_at_the_mirror_and_goes_online(mirror: SetMirror) -> None:
    mirror("https://mirror.example.com/")

    env = registry_mirror.command_env()

    assert env["NPM_CONFIG_REGISTRY"] == "https://mirror.example.com/npm/"
    assert env["YARN_REGISTRY"] == "https://mirror.example.com/npm/"
    assert env["YARN_NPM_REGISTRY_SERVER"] == "https://mirror.example.com/npm"
    assert env["COREPACK_NPM_REGISTRY"] == "https://mirror.example.com/npm"
    assert env["PIP_INDEX_URL"] == "https://mirror.example.com/pypi/simple/"
    assert env["UV_DEFAULT_INDEX"] == "https://mirror.example.com/pypi/simple/"
    # The offline switches the dev images bake are turned off.
    assert env["YARN_ENABLE_NETWORK"] == "true"
    assert env["PIP_NO_INDEX"] == "0"
    assert env["UV_OFFLINE"] == "false"
    # Over HTTPS no client is told to trust a plain-HTTP host.
    assert "PIP_TRUSTED_HOST" not in env and "YARN_UNSAFE_HTTP_WHITELIST" not in env


def test_a_plain_http_in_cluster_mirror_is_trusted_by_host(mirror: SetMirror) -> None:
    mirror("http://registry-mirror:8080")

    env = registry_mirror.command_env()

    assert env["PIP_TRUSTED_HOST"] == "registry-mirror"
    assert env["UV_INSECURE_HOST"] == "registry-mirror"
    assert env["YARN_UNSAFE_HTTP_WHITELIST"] == "registry-mirror"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:token@mirror.example.com",
        "https://mirror.example.com/?token=abc",
        "ftp://mirror.example.com",
        "mirror.example.com",
    ],
)
def test_a_credential_or_malformed_url_is_refused_at_startup(url: str) -> None:
    with pytest.raises(ValueError, match="SANDBOX_REGISTRY_MIRROR_URL"):
        Settings(sandbox_registry_mirror_url=url)


def test_the_setting_is_off_by_default_and_trimmed() -> None:
    assert Settings.model_fields["sandbox_registry_mirror_url"].default == ""
    assert Settings(sandbox_registry_mirror_url=" https://m.example.com/ ").sandbox_registry_mirror_url == (
        "https://m.example.com"
    )


def test_the_shipped_mirror_is_read_only() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")

    # Only GET and HEAD go through; every other method (publish, upload, login) is refused.
    assert "limit_except GET {" in conf
    assert "deny all;" in conf
    # No caller credential reaches a registry.
    assert 'proxy_set_header Authorization "";' in conf
    assert 'proxy_set_header Cookie "";' in conf
    # The three upstreams, and nothing else answered but a refusal.
    for host in ("registry.npmjs.org", "pypi.org", "files.pythonhosted.org"):
        assert f" {host};" in conf
    assert "return 403;" in conf


def test_the_shipped_mirror_resolves_its_upstreams_at_request_time() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")

    # nginx consults the resolver only when proxy_pass holds a variable; a literal host is pinned at startup.
    assert "resolver " in conf
    proxy_passes = [line.strip() for line in conf.splitlines() if line.strip().startswith("proxy_pass ")]
    assert proxy_passes == [
        "proxy_pass https://$npm_upstream$npm_path;",
        "proxy_pass https://$pypi_upstream/simple$pypi_simple_path;",
        "proxy_pass https://$files_upstream$pypi_files_path;",
    ]
    assert "set $npm_upstream registry.npmjs.org;" in conf
    assert "set $pypi_upstream pypi.org;" in conf
    assert "set $files_upstream files.pythonhosted.org;" in conf


def test_the_command_and_service_containers_take_the_mirror() -> None:
    for name in ("commands.py", "services.py"):
        source = (REPO_ROOT / "backend" / "app" / "services" / "sandbox_workspace" / name).read_text(encoding="utf-8")
        assert "egress_allow_list=command_egress()" in source, name
        assert "**command_env()" in source, name
