"""Tests for the embedded runner distribution endpoints (no DB).

The platform image embeds the runner installers / archives built by the
release pipeline and serves them itself — there is no external download URL.
These tests pin the contract:

* ``/dist/{asset}`` serves ONLY allow-listed names (no traversal surface) and
  404s cleanly on a source/dev build without embedded artifacts,
* the download/update manifest reports ``download_configured`` from what is
  actually on disk and only advertises an update when the target archive is
  complete (archive + ``.sha256`` + ``.minisig``) and provably newer,
* the Tauri updater endpoint answers 204 without signed artifacts and a
  well-formed manifest with them,
* ``_external_base`` honours ``X-Forwarded-Proto`` only behind
  ``trust_proxy_headers``, and upgrades a plain-HTTP request-derived base to
  the operator's HTTPS ``BASE_URL`` for the same hostname (mixed-content
  downloads are silently cancelled by browsers) — never the reverse,
  never across hostnames, and never a 500 on a malformed ``BASE_URL``,
* the download surface (``/dist``, ``/download``) accepts HEAD alongside GET
  (probing clients get headers, not a 405).
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import runners as r
from app.core.config import settings

RUNNER_VERSION = "1.260816.0"

_PACKAGE_SIGS = (
    "xtm-one-runner-linux-x64.deb.sig",
    "xtm-one-runner-linux-x64.rpm.sig",
    "xtm-one-runner-linux-arm64.deb.sig",
    "xtm-one-runner-linux-arm64.rpm.sig",
)


def _request(base_url: str = "http://testserver/", **headers) -> SimpleNamespace:
    """A minimal Request stand-in (base_url + headers is all the code reads)."""
    return SimpleNamespace(
        base_url=base_url,
        headers={k.replace("_", "-").lower(): v for k, v in headers.items()},
    )


def _stage_full_dist(tmp_path):
    """Stage a complete signed distribution the way CI does."""
    (tmp_path / "VERSION").write_text(RUNNER_VERSION, encoding="utf-8")
    for name in (
        # One-click installers. Windows and Linux ship one per architecture;
        # the macOS bundle is universal (Intel + Apple Silicon).
        "xtm-one-runner-windows-x64-setup.exe",
        "xtm-one-runner-windows-arm64-setup.exe",
        "xtm-one-runner-macos-universal.dmg",
        "xtm-one-runner-linux-x64.AppImage",
        "xtm-one-runner-linux-x64.deb",
        "xtm-one-runner-linux-x64.rpm",
        "xtm-one-runner-linux-arm64.AppImage",
        "xtm-one-runner-linux-arm64.deb",
        "xtm-one-runner-linux-arm64.rpm",
        # Bare archives + integrity + signature.
        "xtm-one-runner-windows-x64.zip",
        "xtm-one-runner-windows-x64.zip.sha256",
        "xtm-one-runner-windows-x64.zip.minisig",
        "xtm-one-runner-windows-arm64.zip",
        "xtm-one-runner-windows-arm64.zip.sha256",
        "xtm-one-runner-windows-arm64.zip.minisig",
        "xtm-one-runner-linux-x64.tar.gz",
        "xtm-one-runner-linux-x64.tar.gz.sha256",
        "xtm-one-runner-linux-x64.tar.gz.minisig",
        "xtm-one-runner-linux-arm64.tar.gz",
        "xtm-one-runner-linux-arm64.tar.gz.sha256",
        "xtm-one-runner-linux-arm64.tar.gz.minisig",
        # Headless flavor: bare archives only, one per Linux architecture.
        "xtm-one-runner-linux-x64-headless.tar.gz",
        "xtm-one-runner-linux-x64-headless.tar.gz.sha256",
        "xtm-one-runner-linux-x64-headless.tar.gz.minisig",
        "xtm-one-runner-linux-arm64-headless.tar.gz",
        "xtm-one-runner-linux-arm64-headless.tar.gz.sha256",
        "xtm-one-runner-linux-arm64-headless.tar.gz.minisig",
        "xtm-one-runner-macos-universal.tar.gz",
        "xtm-one-runner-macos-universal.tar.gz.sha256",
        "xtm-one-runner-macos-universal.tar.gz.minisig",
        # Headless install scripts.
        "xtm-one-runner-installer.sh",
        "xtm-one-runner-installer.ps1",
    ):
        (tmp_path / name).write_bytes(b"artifact-bytes")
    # Signed Tauri updater artifacts (both Linux legs). Tauri v2 form: the
    # updater artifact IS the AppImage (staged above) plus a detached .sig —
    # the v1-era .AppImage.tar.gz pair never exists in this pipeline.
    # The packages carry their own .sig: a .deb / .rpm install only accepts
    # its own format.
    for name in (
        "xtm-one-runner-linux-x64.AppImage.sig",
        "xtm-one-runner-linux-arm64.AppImage.sig",
        *_PACKAGE_SIGS,
    ):
        (tmp_path / name).write_text("minisign-signature-b64", encoding="utf-8")


@pytest.fixture
def dist_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "runner_dist_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def no_dist(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "runner_dist_dir", str(tmp_path / "absent"))


# ── /dist/{asset} allow-list ─────────────────────────────────────────────────


async def test_dist_rejects_names_outside_the_allow_list(dist_dir):
    _stage_full_dist(dist_dir)
    for name in ("evil.sh", "../../etc/passwd", "..\\..\\secrets", "VERSION"):
        with pytest.raises(HTTPException) as exc:
            await r.download_dist_asset(name)
        assert exc.value.status_code == 404


async def test_dist_404_when_not_embedded(no_dist):
    with pytest.raises(HTTPException) as exc:
        await r.download_dist_asset("xtm-one-runner-windows-x64-setup.exe")
    assert exc.value.status_code == 404
    assert "without embedded runner artifacts" in exc.value.detail


async def test_dist_serves_a_staged_artifact(dist_dir):
    _stage_full_dist(dist_dir)
    resp = await r.download_dist_asset("xtm-one-runner-windows-x64-setup.exe")
    assert resp.path == dist_dir / "xtm-one-runner-windows-x64-setup.exe"
    assert "xtm-one-runner-windows-x64-setup.exe" in resp.headers["content-disposition"]


def test_allow_list_covers_integrity_and_signature_siblings():
    assets = r._DIST_ASSETS
    assert "xtm-one-runner-linux-x64.tar.gz.sha256" in assets
    assert "xtm-one-runner-linux-x64.tar.gz.minisig" in assets
    assert "xtm-one-runner-linux-x64.AppImage.sig" in assets
    # Each Linux package carries the signature its own updater verifies.
    for sig in _PACKAGE_SIGS:
        assert sig in assets
    assert "xtm-one-runner-installer.sh" in assets
    # ARM builds are first-class: installers, packages and bare archives.
    assert "xtm-one-runner-linux-arm64.AppImage" in assets
    assert "xtm-one-runner-linux-arm64.deb" in assets
    assert "xtm-one-runner-windows-arm64-setup.exe" in assets
    assert "xtm-one-runner-windows-arm64.zip.sha256" in assets
    assert "xtm-one-runner-linux-arm64.tar.gz.minisig" in assets
    assert "xtm-one-runner-linux-x64-headless.tar.gz" in assets
    assert "xtm-one-runner-linux-arm64-headless.tar.gz.minisig" in assets
    # The v1-era Linux updater names must stay gone: CI never produces them
    # with createUpdaterArtifacts: true, and advertising one bricked the
    # first signed deploys.
    assert "xtm-one-runner-linux-x64.AppImage.tar.gz" not in assets
    assert "xtm-one-runner-linux-x64.AppImage.tar.gz.sig" not in assets
    # The VERSION marker is internal — never directly downloadable.
    assert "VERSION" not in assets


# ── Download / update manifest ───────────────────────────────────────────────


async def test_manifest_reports_unconfigured_without_dist(no_dist):
    out = await r.update_manifest(_request(), None, None, None)
    assert out["download_configured"] is False
    assert out["runner_version"] is None
    assert out["artifacts"] == {}
    assert all(p["installer_url"] is None for p in out["platforms"].values())
    # No embedded scripts -> the UI must not render headless install commands.
    assert out["install_scripts"] == {"sh": None, "ps1": None}


async def test_manifest_advertises_embedded_artifacts(dist_dir):
    _stage_full_dist(dist_dir)
    out = await r.update_manifest(_request(), None, None, None)
    assert out["download_configured"] is True
    assert out["runner_version"] == RUNNER_VERSION
    assert out["platforms"]["windows"]["installer_url"] == (
        "http://testserver/api/v1/runners/dist/xtm-one-runner-windows-x64-setup.exe"
    )
    assert out["platforms"]["linux"]["formats"]["deb"].endswith(
        "/dist/xtm-one-runner-linux-x64.deb"
    )
    win = out["artifacts"]["windows"]["x64"]
    assert win["url"].endswith("/dist/xtm-one-runner-windows-x64.zip")
    assert win["sha256_url"].endswith(".sha256")
    assert win["signature_url"].endswith(".minisig")
    # The universal macOS archive serves both architectures.
    assert out["artifacts"]["macos"]["arm64"]["url"].endswith(
        "xtm-one-runner-macos-universal.tar.gz"
    )
    # Headless install scripts are advertised for the UI's CLI instructions.
    assert out["install_scripts"]["sh"] == (
        "http://testserver/api/v1/runners/dist/xtm-one-runner-installer.sh"
    )
    assert out["install_scripts"]["ps1"].endswith(
        "/dist/xtm-one-runner-installer.ps1"
    )


async def test_manifest_target_update_decision(dist_dir):
    _stage_full_dist(dist_dir)
    older = await r.update_manifest(_request(), "linux", "x64", "1.260101.0")
    assert older["target"]["update_available"] is True
    assert older["target"]["version"] == RUNNER_VERSION

    same = await r.update_manifest(_request(), "linux", "x64", RUNNER_VERSION)
    assert same["target"]["update_available"] is False

    newer = await r.update_manifest(_request(), "linux", "x64", "9.999999.9")
    assert newer["target"]["update_available"] is False


async def test_manifest_never_offers_an_unsigned_update(dist_dir):
    _stage_full_dist(dist_dir)
    (dist_dir / "xtm-one-runner-linux-x64.tar.gz.minisig").unlink()
    out = await r.update_manifest(_request(), "linux", "x64", "1.260101.0")
    # The archive is still downloadable, but no self-update is advertised:
    # the runner refuses unsigned code, so offering it would only retry-loop.
    assert out["target"]["url"] is not None
    assert out["target"]["signature_url"] is None
    assert out["target"]["update_available"] is False


async def test_manifest_target_needs_update_uses_protocol_floor(
    dist_dir, monkeypatch
):
    _stage_full_dist(dist_dir)
    monkeypatch.setattr(settings, "runner_min_supported_version", "1.260501.0")
    out = await r.update_manifest(_request(), "linux", "x64", "1.260101.0")
    assert out["target"]["needs_update"] is True
    ok = await r.update_manifest(_request(), "linux", "x64", RUNNER_VERSION)
    assert ok["target"]["needs_update"] is False


# ── Download redirects ───────────────────────────────────────────────────────


async def test_download_redirects_to_the_dist_asset(dist_dir):
    _stage_full_dist(dist_dir)
    resp = await r.download_runner("windows")
    assert resp.status_code == 307
    assert resp.headers["location"] == (
        "/api/v1/runners/dist/xtm-one-runner-windows-x64-setup.exe"
    )
    resp = await r.download_runner_format("linux", "deb")
    assert resp.headers["location"].endswith("xtm-one-runner-linux-x64.deb")
    resp = await r.download_runner_format("macos", "arm64")
    assert resp.headers["location"].endswith("xtm-one-runner-macos-universal.tar.gz")


async def test_download_404s_cleanly_without_dist(no_dist):
    with pytest.raises(HTTPException) as exc:
        await r.download_runner("windows")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await r.download_runner_format("linux", "deb")
    assert exc.value.status_code == 404


async def test_download_unknown_platform_or_format(dist_dir):
    _stage_full_dist(dist_dir)
    with pytest.raises(HTTPException):
        await r.download_runner("beos")
    with pytest.raises(HTTPException):
        await r.download_runner_format("linux", "snap")
    # A platform we ship, on an architecture we do not.
    with pytest.raises(HTTPException) as exc:
        await r.download_runner("linux", arch="riscv64")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        await r.download_runner_format("linux", "deb", arch="riscv64")


async def test_download_serves_arm64_builds(dist_dir):
    """ARM machines get an ARM build, never the x86_64 one."""
    _stage_full_dist(dist_dir)
    resp = await r.download_runner("linux", arch="arm64")
    assert resp.headers["location"].endswith("xtm-one-runner-linux-arm64.AppImage")
    resp = await r.download_runner("windows", arch="arm64")
    assert resp.headers["location"].endswith(
        "xtm-one-runner-windows-arm64-setup.exe"
    )
    # ?arch= also selects the alternate Linux package formats.
    resp = await r.download_runner_format("linux", "deb", arch="arm64")
    assert resp.headers["location"].endswith("xtm-one-runner-linux-arm64.deb")
    # The bare archive the headless installer fetches as /download/{os}/{arch}.
    resp = await r.download_runner_format("linux", "arm64")
    assert resp.headers["location"].endswith("xtm-one-runner-linux-arm64.tar.gz")
    resp = await r.download_runner_format("windows", "arm64")
    assert resp.headers["location"].endswith("xtm-one-runner-windows-arm64.zip")
    # macOS stays universal: both architectures resolve to the one bundle.
    resp = await r.download_runner("macos", arch="arm64")
    assert resp.headers["location"].endswith("xtm-one-runner-macos-universal.dmg")


async def test_manifest_exposes_per_architecture_downloads(dist_dir):
    _stage_full_dist(dist_dir)
    out = await r.update_manifest(_request(), None, None, None)
    linux = out["platforms"]["linux"]
    # The flat fields stay the x64 build (the shape the UI already reads).
    assert linux["installer_url"].endswith("xtm-one-runner-linux-x64.AppImage")
    assert linux["universal"] is False
    arm = linux["architectures"]["arm64"]
    assert arm["installer_url"].endswith("xtm-one-runner-linux-arm64.AppImage")
    assert arm["formats"]["rpm"].endswith("xtm-one-runner-linux-arm64.rpm")
    assert out["platforms"]["windows"]["architectures"]["arm64"][
        "installer_url"
    ].endswith("xtm-one-runner-windows-arm64-setup.exe")
    # One universal binary for every Mac: the UI must not offer it twice.
    assert out["platforms"]["macos"]["universal"] is True
    # Bare archives for the headless installer + daemon self-updater.
    for os_key, archive in (
        ("linux", "xtm-one-runner-linux-arm64.tar.gz"),
        ("windows", "xtm-one-runner-windows-arm64.zip"),
    ):
        entry = out["artifacts"][os_key]["arm64"]
        assert entry["url"].endswith(archive)
        assert entry["sha256_url"].endswith(".sha256")
        assert entry["signature_url"].endswith(".minisig")


async def test_download_serves_the_headless_flavor(dist_dir):
    """A server takes the build that links no desktop stack."""
    _stage_full_dist(dist_dir)
    resp = await r.download_runner_format("linux", "headless")
    assert resp.headers["location"].endswith(
        "xtm-one-runner-linux-x64-headless.tar.gz"
    )
    resp = await r.download_runner_format("linux", "headless", arch="arm64")
    assert resp.headers["location"].endswith(
        "xtm-one-runner-linux-arm64-headless.tar.gz"
    )
    with pytest.raises(HTTPException) as exc:
        await r.download_runner_format("linux", "headless", arch="riscv64")
    assert exc.value.status_code == 404
    # Not a Windows/macOS thing: those service installs run the desktop build.
    with pytest.raises(HTTPException):
        await r.download_runner_format("windows", "headless")


async def test_manifest_keeps_a_headless_runner_on_headless_builds(dist_dir):
    _stage_full_dist(dist_dir)
    out = await r.update_manifest(_request(), None, None, None)
    entry = out["artifacts"]["linux"]["arm64"]
    # The flavors hang off one (os, arch) entry: same machine, other build.
    assert entry["url"].endswith("xtm-one-runner-linux-arm64.tar.gz")
    assert entry["headless"]["url"].endswith(
        "xtm-one-runner-linux-arm64-headless.tar.gz"
    )
    assert entry["headless"]["signature_url"].endswith(".minisig")

    headless = await r.update_manifest(
        _request(), "linux", "x64", "1.260101.0", "headless"
    )
    assert headless["target"]["flavor"] == "headless"
    assert headless["target"]["update_available"] is True
    assert headless["target"]["url"].endswith(
        "xtm-one-runner-linux-x64-headless.tar.gz"
    )
    # An unqualified check still means the desktop build.
    desktop = await r.update_manifest(_request(), "linux", "x64", "1.260101.0")
    assert desktop["target"]["flavor"] == "desktop"
    assert desktop["target"]["url"].endswith("xtm-one-runner-linux-x64.tar.gz")


async def test_manifest_never_offers_a_desktop_build_to_a_headless_runner(dist_dir):
    """No headless archive embedded -> no target at all, not the desktop one.

    Handing a server the desktop build would swap a working runner for one that
    dies in the loader on the next start.
    """
    _stage_full_dist(dist_dir)
    (dist_dir / "xtm-one-runner-linux-x64-headless.tar.gz").unlink()
    out = await r.update_manifest(
        _request(), "linux", "x64", "1.260101.0", "headless"
    )
    assert out["target"]["url"] is None
    assert out["target"]["update_available"] is False
    assert "headless" not in out["artifacts"]["linux"]["x64"]


async def test_manifest_rejects_an_unknown_flavor(dist_dir):
    """Fail closed: an unrecognised flavor must never silently mean desktop.

    Defaulting on a typo would hand a headless server the desktop archive - the
    one build that cannot start there - which is the failure this whole flavor
    mechanism exists to prevent.
    """
    _stage_full_dist(dist_dir)
    for bogus in ("Headles", "gui", "headless-x64"):
        with pytest.raises(HTTPException) as exc:
            await r.update_manifest(_request(), "linux", "x64", "1.260101.0", bogus)
        assert exc.value.status_code == 400
    # Case and stray whitespace are normalised, not rejected: they are the
    # shapes a hand-typed or proxy-mangled query actually takes, and they name
    # a flavor we ship.
    for ok in ("Headless", " headless "):
        out = await r.update_manifest(_request(), "linux", "x64", "1.260101.0", ok)
        assert out["target"]["flavor"] == "headless"
        assert out["target"]["url"].endswith("-headless.tar.gz")
    # Omitted (or empty) still means desktop: runners predating the flavor
    # never send one, and they are all desktop builds.
    for absent in (None, ""):
        out = await r.update_manifest(_request(), "linux", "x64", "1.260101.0", absent)
        assert out["target"]["flavor"] == "desktop"
        assert out["target"]["url"].endswith("xtm-one-runner-linux-x64.tar.gz")


async def test_manifest_offers_an_arm64_self_update(dist_dir):
    _stage_full_dist(dist_dir)
    out = await r.update_manifest(_request(), "linux", "arm64", "1.260101.0")
    assert out["target"]["update_available"] is True
    assert out["target"]["url"].endswith("xtm-one-runner-linux-arm64.tar.gz")


# ── Tauri updater endpoint ───────────────────────────────────────────────────


async def test_updater_204_without_signed_artifacts(no_dist):
    resp = await r.runner_updater(_request(), "linux-x86_64", "x64", "1.0.0")
    assert resp.status_code == 204


async def test_updater_manifest_from_signed_artifacts(dist_dir):
    _stage_full_dist(dist_dir)
    out = await r.runner_updater(_request(), "linux-x86_64", "x64", "1.0.0")
    assert out["version"] == RUNNER_VERSION
    entry = out["platforms"]["linux-x86_64"]
    assert entry["signature"] == "minisign-signature-b64"
    # v2 form: the GUI updater downloads the raw AppImage CI signed.
    assert entry["url"].endswith("xtm-one-runner-linux-x64.AppImage")
    # The ARM leg is advertised under its own Tauri target key, so a runner
    # on an arm64 box self-updates instead of being offered an x86_64 build.
    assert out["platforms"]["linux-aarch64"]["url"].endswith(
        "xtm-one-runner-linux-arm64.AppImage"
    )
    # Only the platform whose .sig exists is advertised.
    assert "windows-x86_64" not in out["platforms"]
    assert "windows-aarch64" not in out["platforms"]


async def test_updater_manifest_serves_each_linux_package_its_own_artifact(dist_dir):
    """A .deb / .rpm install looks up linux-<arch>-<bundle> first and refuses
    any other format, so each package kind gets its own entry."""
    _stage_full_dist(dist_dir)
    out = await r.runner_updater(_request(), "linux-x86_64", "x64", "1.0.0")
    platforms = out["platforms"]
    for arch, key in (("x64", "x86_64"), ("arm64", "aarch64")):
        for fmt in ("deb", "rpm"):
            entry = platforms[f"linux-{key}-{fmt}"]
            assert entry["url"].endswith(f"xtm-one-runner-linux-{arch}.{fmt}")
            assert entry["signature"] == "minisign-signature-b64"
        # The bare key stays the AppImage: the AppImage install's fallback.
        assert platforms[f"linux-{key}"]["url"].endswith(f"xtm-one-runner-linux-{arch}.AppImage")


async def test_updater_manifest_skips_a_package_without_its_signature(dist_dir):
    """An unsigned package is never offered (the updater would refuse it)."""
    _stage_full_dist(dist_dir)
    (dist_dir / "xtm-one-runner-linux-x64.rpm.sig").unlink()
    out = await r.runner_updater(_request(), "linux-x86_64", "x64", "1.0.0")
    assert "linux-x86_64-rpm" not in out["platforms"]
    assert "linux-x86_64-deb" in out["platforms"]


async def test_dist_serves_a_package_signature(dist_dir):
    _stage_full_dist(dist_dir)
    resp = await r.download_dist_asset("xtm-one-runner-linux-x64.deb.sig")
    assert resp.path == dist_dir / "xtm-one-runner-linux-x64.deb.sig"


def test_updater_keys_follow_the_tauri_lookup_grammar():
    """The updater matches manifest keys literally: ``{os}-{arch}-{installer}``
    first, then ``{os}-{arch}``. A key outside that grammar is never looked up,
    and an installer entry needs the fallback the other install kinds use."""
    for key in r._UPDATER_ARTIFACTS:
        os_part, arch_part = key.split("-")
        assert os_part in {"windows", "darwin", "linux"}, key
        assert arch_part in {"x86_64", "aarch64"}, key
    for key in r._UPDATER_INSTALLER_ARTIFACTS:
        os_part, arch_part, installer = key.split("-")
        assert installer in {"appimage", "deb", "rpm", "app", "msi", "nsis"}, key
        assert f"{os_part}-{arch_part}" in r._UPDATER_ARTIFACTS, key


# ── External base URL resolution ─────────────────────────────────────────────


def test_external_base_ignores_forwarded_proto_by_default(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    req = _request("http://xtm.example.com/", x_forwarded_proto="https")
    assert r._external_base(req) == "http://xtm.example.com"


def test_external_base_honours_forwarded_proto_when_trusted(monkeypatch):
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    req = _request("http://xtm.example.com/", x_forwarded_proto="https")
    assert r._external_base(req) == "https://xtm.example.com"


def test_external_base_upgrades_to_configured_https_base(monkeypatch):
    """TLS terminated upstream, proxy headers NOT trusted: the request-derived
    base is plain HTTP, but the canonical BASE_URL is HTTPS for the same host.
    Emitting http:// here hands the browser a mixed-content download that it
    cancels silently — so the base upgrades to BASE_URL."""
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    monkeypatch.setattr(settings, "base_url", "https://xtm.example.com")
    req = _request("http://xtm.example.com/")
    assert r._external_base(req) == "https://xtm.example.com"
    # Hostname match is case-insensitive; a request port is superseded by the
    # canonical netloc.
    req = _request("http://XTM.Example.Com:4000/")
    assert r._external_base(req) == "https://xtm.example.com"


def test_external_base_never_upgrades_a_foreign_host(monkeypatch):
    """Split-horizon reach (internal hostname) keeps the request-derived base:
    installers must download from a host the caller can actually resolve."""
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    monkeypatch.setattr(settings, "base_url", "https://xtm.example.com")
    req = _request("http://internal.local:4000/")
    assert r._external_base(req) == "http://internal.local:4000"


def test_external_base_never_downgrades_https(monkeypatch):
    """A misconfigured plain-HTTP BASE_URL must never strip TLS from a base
    that arrived over HTTPS."""
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    monkeypatch.setattr(settings, "base_url", "http://xtm.example.com")
    req = _request("https://xtm.example.com/")
    assert r._external_base(req) == "https://xtm.example.com"


def test_external_base_never_downgrades_an_https_derivation(monkeypatch):
    """Same rule for the header-derived scheme: an http BASE_URL must not
    replace a base that a trusted ``X-Forwarded-Proto`` already upgraded -
    the BASE_URL adoption runs after the header upgrade and respects it."""
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    monkeypatch.setattr(settings, "base_url", "http://xtm.example.com")
    req = _request("http://xtm.example.com/", x_forwarded_proto="https")
    assert r._external_base(req) == "https://xtm.example.com"


def test_external_base_ignores_an_http_configured_base(monkeypatch):
    """An http-scheme BASE_URL adds nothing over the request-derived base:
    no scheme change, and no host/port rewriting either."""
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    monkeypatch.setattr(settings, "base_url", "http://xtm.example.com")
    req = _request("http://xtm.example.com:4000/")
    assert r._external_base(req) == "http://xtm.example.com:4000"


def test_external_base_survives_a_malformed_configured_base(monkeypatch):
    """``urlsplit`` refuses an unbalanced IPv6 bracket with ValueError. A
    typoed BASE_URL (or a hostile Host header reaching the raw request) must
    degrade to the request-derived base - the manifest/updater endpoints are
    unauthenticated, so an unhandled parse error would 500 them for every
    caller."""
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    monkeypatch.setattr(settings, "base_url", "https://[oops")
    req = _request("http://xtm.example.com/")
    assert r._external_base(req) == "http://xtm.example.com"


async def test_manifest_urls_are_https_behind_a_tls_terminating_proxy(
    dist_dir, monkeypatch
):
    """End to end: the manifest a TLS-terminated deployment serves
    (plain-http request, https BASE_URL, proxy headers untrusted) advertises
    https URLs everywhere - installers, bare archives and install scripts."""
    _stage_full_dist(dist_dir)
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    monkeypatch.setattr(settings, "base_url", "https://xtm.example.com")
    out = await r.update_manifest(
        _request("http://xtm.example.com/"), None, None, None
    )
    assert out["platforms"]["windows"]["installer_url"].startswith(
        "https://xtm.example.com/api/v1/runners/dist/"
    )
    assert out["artifacts"]["linux"]["x64"]["url"].startswith(
        "https://xtm.example.com/"
    )
    assert out["install_scripts"]["sh"].startswith("https://xtm.example.com/")


# ── Download-surface HTTP methods ────────────────────────────────────────────


def test_download_surface_accepts_head():
    """Download managers, link checkers and browser extensions probe with HEAD
    before fetching; a 405 kills an intercepted download with no visible error.
    FileResponse serves HEAD (headers only) natively once routed."""
    methods = {
        route.path: set(route.methods or ())
        for route in r.router.routes
        if hasattr(route, "methods")
    }
    for path in ("/dist/{asset}", "/download/{platform}", "/download/{platform}/{fmt}"):
        assert {"GET", "HEAD"} <= methods[path], path


# -- the artifact-name contract --------------------------------------
#
# release-stage.sh (the release's runner-build), the install scripts and the tables above all
# spell the same file names; a rename on one side 404s the downloads and
# self-updates of the other. Every name the platform serves follows one shape,
# and the OS / architecture it spells must be the one its table key says.

REPO_ROOT = Path(__file__).resolve().parents[2]
_ASSET_NAME = re.compile(r"^xtm-one-runner-(windows|macos|linux)-(x64|arm64|universal)[.-]")
_UPDATER_OS = {"windows": "windows", "darwin": "macos", "linux": "linux"}
_UPDATER_ARCH = {"x86_64": "x64", "aarch64": "arm64"}


def _served_assets() -> list[tuple[str, str, str, str]]:
    assets = []
    for table_name in ("_PLATFORM_INSTALLER", "_BARE_ARCHIVES", "_HEADLESS_ARCHIVES"):
        for (os_key, arch), name in getattr(r, table_name).items():
            assets.append((table_name, os_key, arch, name))
    for (arch, _fmt), name in r._LINUX_FORMATS.items():
        assets.append(("_LINUX_FORMATS", "linux", arch, name))
    for key, name in r._UPDATER_ARTIFACTS.items():
        os_part, arch_part = key.split("-")
        assets.append(("_UPDATER_ARTIFACTS", _UPDATER_OS[os_part], _UPDATER_ARCH[arch_part], name))
    for key, name in r._UPDATER_INSTALLER_ARTIFACTS.items():
        os_part, arch_part, _installer = key.split("-")
        assets.append(("_UPDATER_INSTALLER_ARTIFACTS", _UPDATER_OS[os_part], _UPDATER_ARCH[arch_part], name))
    return assets


@pytest.mark.parametrize(
    ("table_name", "os_key", "arch", "name"),
    _served_assets(),
    ids=lambda value: str(value),
)
def test_every_distribution_asset_follows_the_os_arch_convention(table_name, os_key, arch, name):
    match = _ASSET_NAME.match(name)
    assert match, f"{table_name}: {name!r} does not follow xtm-one-runner-<os>-<arch>"
    # macOS ships one universal binary for both architectures.
    expected = (os_key, "universal" if os_key == "macos" else arch)
    assert match.groups() == expected, f"{table_name}: {name!r} is filed under {expected}"


def test_the_release_stages_every_asset_the_platform_serves():
    # The release workflow (XTM-One-Platform/release) stages the runner with this
    # script, from the commit it releases, one leg per OS and architecture: each
    # name above, its architecture as the script's $arch, is one it writes.
    stage = (REPO_ROOT / "apps/runner/scripts/release-stage.sh").read_text(encoding="utf-8")
    for table_name, _os_key, _arch, name in _served_assets():
        template = re.sub(r"-(x64|arm64|universal)([.-])", r"-$arch\2", name, count=1)
        assert f'"$out/{template}"' in stage, f"{table_name}: release-stage.sh does not stage {name}"
    assert 'printf \'%s\' "$version" > "$out/VERSION"' in stage


def test_every_supported_platform_has_a_named_asset_per_architecture():
    for table in (r._PLATFORM_INSTALLER, r._BARE_ARCHIVES):
        for os_key in r._RUNNER_PLATFORMS_WITH_DIST:
            for arch in r._RUNNER_ARCHES:
                assert (os_key, arch) in table
    for arch in r._RUNNER_ARCHES:
        for fmt in r._LINUX_FORMAT_NAMES:
            assert (arch, fmt) in r._LINUX_FORMATS
        # The headless flavor is Linux only, but on every architecture.
        assert ("linux", arch) in r._HEADLESS_ARCHIVES
    # One GUI self-update artifact per supported platform and architecture.
    updater_keys = {
        (_UPDATER_OS[os_part], _UPDATER_ARCH[arch_part])
        for os_part, arch_part in (key.split("-") for key in r._UPDATER_ARTIFACTS)
    }
    for os_key in r._RUNNER_PLATFORMS_WITH_DIST:
        for arch in r._RUNNER_ARCHES:
            assert (os_key, arch) in updater_keys


def test_every_linux_package_updates_from_its_own_package():
    # A .deb / .rpm install takes nothing but its own package: the
    # desktop app skips any other extension (gui.rs `update_installs_on`), and a
    # wrong architecture only fails in dpkg / rpm, behind the password prompt.
    # So each entry is the package the downloads offer for its architecture, and
    # every package has one.
    served = {}
    for key, name in r._UPDATER_INSTALLER_ARTIFACTS.items():
        os_part, arch_part, installer = key.split("-")
        assert os_part == "linux", key
        assert name.endswith(f".{installer}"), key
        served[(_UPDATER_ARCH[arch_part], installer)] = name
    for arch in r._RUNNER_ARCHES:
        for fmt in ("deb", "rpm"):
            assert served.get((arch, fmt)) == r._LINUX_FORMATS[(arch, fmt)], (arch, fmt)
