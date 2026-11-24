"""The command and service containers reach npm, PyPI and Maven Central through the read-only mirror only (#25, #1227)."""

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
    assert registry_mirror.maven_settings_xml() == ""


def test_the_mirror_host_alone_joins_the_egress_list(mirror: SetMirror) -> None:
    mirror("https://registry-mirror.sandbox.svc:8443", allow="api.example.com")

    egress = registry_mirror.command_egress()

    assert egress == frozenset({"api.example.com", "registry-mirror.sandbox.svc"})
    assert not egress & {
        "registry.npmjs.org",
        "pypi.org",
        "files.pythonhosted.org",
        "registry.yarnpkg.com",
        "repo.maven.apache.org",
    }


def test_every_package_client_points_at_the_mirror_and_goes_online(mirror: SetMirror) -> None:
    mirror("https://mirror.example.com/")

    env = registry_mirror.command_env()

    assert env["NPM_CONFIG_REGISTRY"] == "https://mirror.example.com/npm/"
    assert env["YARN_REGISTRY"] == "https://mirror.example.com/npm/"
    assert env["YARN_NPM_REGISTRY_SERVER"] == "https://mirror.example.com/npm"
    assert env["COREPACK_NPM_REGISTRY"] == "https://mirror.example.com/npm"
    assert env["PIP_INDEX_URL"] == "https://mirror.example.com/pypi/simple/"
    assert env["UV_DEFAULT_INDEX"] == "https://mirror.example.com/pypi/simple/"
    # Poetry ignores PIP_INDEX_URL: the toolchain-python plugin reads this one (#1250).
    assert env["POETRY_PYPI_MIRROR_URL"] == "https://mirror.example.com/pypi/simple/"
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
    # The four upstreams, and nothing else answered but a refusal.
    for host in ("registry.npmjs.org", "pypi.org", "files.pythonhosted.org", "repo.maven.apache.org"):
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
        "proxy_pass https://$maven_upstream/maven2$maven_path;",
    ]
    assert "set $npm_upstream registry.npmjs.org;" in conf
    assert "set $pypi_upstream pypi.org;" in conf
    assert "set $files_upstream files.pythonhosted.org;" in conf
    assert "set $maven_upstream repo.maven.apache.org;" in conf


def test_every_mirror_route_is_read_only_and_drops_credentials() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")

    # Each proxied location refuses every method but GET (mvn deploy is a PUT) and forwards no credential.
    for route in ("/npm/", "/pypi/simple/", "/pypi/files/", "/maven/"):
        block = conf.split(f"location {route} {{", 1)[1].split("\n        }", 1)[0]
        assert "limit_except GET {" in block, route
        assert "deny all;" in block, route
        assert 'proxy_set_header Authorization "";' in block, route
        assert 'proxy_set_header Cookie "";' in block, route
        assert "proxy_ssl_verify on;" in block, route
    # The header states the Maven route in the security boundary.
    assert "repo.maven.apache.org/maven2" in conf.split("worker_processes", 1)[0]


def test_maven_mirrors_every_repository_to_the_maven_route(mirror: SetMirror) -> None:
    from defusedxml import ElementTree  # type: ignore[import-untyped]

    mirror("http://registry-mirror:8080/")

    env = registry_mirror.command_env()
    root = ElementTree.fromstring(registry_mirror.maven_settings_xml())
    ns = {"m": "http://maven.apache.org/SETTINGS/1.2.0"}
    mirrors = root.findall("m:mirrors/m:mirror", ns)

    assert env["MAVEN_ARGS"] == f"--global-settings {registry_mirror.MAVEN_SETTINGS_PATH}"
    assert len(mirrors) == 1
    assert mirrors[0].findtext("m:mirrorOf", namespaces=ns) == "*"
    assert mirrors[0].findtext("m:url", namespaces=ns) == "http://registry-mirror:8080/maven/"
    # The settings carry no server credential and no proxy: the mirror is anonymous.
    assert root.find("m:servers", ns) is None and root.find("m:proxies", ns) is None


async def test_the_maven_settings_are_written_only_with_a_mirror(mirror: SetMirror) -> None:
    written: dict[str, bytes] = {}

    class _Files:
        async def write_file(self, path: str, data: bytes) -> None:
            written[path] = data

    from types import SimpleNamespace

    sandbox = SimpleNamespace(files=_Files())
    mirror("")
    await registry_mirror.write_maven_settings(sandbox)
    assert written == {}

    mirror("https://mirror.example.com")
    await registry_mirror.write_maven_settings(sandbox)
    assert b"<url>https://mirror.example.com/maven/</url>" in written[registry_mirror.MAVEN_SETTINGS_PATH]


def test_the_command_and_service_containers_take_the_mirror() -> None:
    for name in ("commands.py", "services.py"):
        source = (REPO_ROOT / "backend" / "app" / "services" / "sandbox_workspace" / name).read_text(encoding="utf-8")
        assert "egress_allow_list=command_egress()" in source, name
        assert "**command_env()" in source, name


class _FakeExecd:
    """Records the options of each command the code starts in the container."""

    def __init__(self) -> None:
        self.envs: list[dict[str, str]] = []

    async def run(self, _command: str, *, opts) -> None:
        self.envs.append(dict(opts.envs or {}))


class _FakeFiles:
    def __init__(self) -> None:
        self.paths: list[str] = []

    async def create_directories(self, _entries) -> None:
        return None

    async def write_file(self, path: str, _data: bytes) -> None:
        self.paths.append(path)


class _FakeSandbox:
    def __init__(self) -> None:
        self.commands = _FakeExecd()
        self.files = _FakeFiles()


async def test_a_command_process_gets_the_mirror_variables(mirror: SetMirror) -> None:
    # A pooled container (poolRef) does not hand the create's env to the process execd starts: the
    # mirror's variables must travel with the command itself, or corepack goes to repo.yarnpkg.com.
    from types import SimpleNamespace

    from app.services.sandbox_workspace import commands

    mirror("http://registry-mirror.ns.svc.cluster.local:8080")
    sandbox = _FakeSandbox()
    command = SimpleNamespace(argv=("yarn", "install"), env=(), cwd="", timeout=60, report_path=None)

    await commands._start(sandbox, command, "run1", {})

    (env,) = sandbox.commands.envs
    assert env["COREPACK_NPM_REGISTRY"] == "http://registry-mirror.ns.svc.cluster.local:8080/npm"
    assert env["YARN_NPM_REGISTRY_SERVER"] == "http://registry-mirror.ns.svc.cluster.local:8080/npm"
    assert env["COREPACK_ENABLE_NETWORK"] == "1"
    assert env["CI"] == commands.SAFE_ENV["CI"]
    # The Maven settings MAVEN_ARGS names are in the container before the command starts.
    assert registry_mirror.MAVEN_SETTINGS_PATH in sandbox.files.paths


async def test_a_command_without_a_mirror_keeps_the_safe_env_alone(mirror: SetMirror) -> None:
    from types import SimpleNamespace

    from app.services.sandbox_workspace import commands

    mirror("")
    sandbox = _FakeSandbox()
    command = SimpleNamespace(argv=("yarn",), env=(), cwd="", timeout=60, report_path=None)

    await commands._start(sandbox, command, "run1", {})

    assert sandbox.commands.envs == [commands.SAFE_ENV]
    assert registry_mirror.MAVEN_SETTINGS_PATH not in sandbox.files.paths


async def test_a_service_process_gets_the_mirror_variables(mirror: SetMirror) -> None:
    from types import SimpleNamespace

    from app.services.sandbox_workspace import services

    mirror("https://mirror.example.com")
    sandbox = _FakeSandbox()
    spec = SimpleNamespace(name="web", argv=("yarn", "dev"), cwd="", env=(("PORT", "3000"),))
    resolved = SimpleNamespace(checkout="repo")

    await services._launch(sandbox, resolved, spec)

    (env,) = sandbox.commands.envs
    assert env["COREPACK_NPM_REGISTRY"] == "https://mirror.example.com/npm"
    assert env["PORT"] == "3000"
    assert sandbox.files.paths == [registry_mirror.MAVEN_SETTINGS_PATH]
