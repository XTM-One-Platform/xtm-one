"""Behaviour of the runner install scripts.

``apps/runner/dist/install.sh`` (``curl ... | bash``) and ``install.ps1`` are
served by every release image. These tests run the real scripts against a
local HTTP server standing in for the platform (``--server`` / ``-Server``) or
a mirror (``--download-base``), and pin what an operator is told:

* an install whose SHA-256 could not be verified says so loudly and names the
  cause (no hashing tool on the host, no ``.sha256`` next to the archive, or an
  empty one), and ``--require-checksum`` / ``XTM_RUNNER_REQUIRE_CHECKSUM=1``
  refuses it; a mismatch, or a ``.sha256`` that holds no digest, is refused
  whatever the flag, and every form the self-updater reads verifies;
* a failed platform probe names its HTTP status, and only a 404 is reported as
  "no embedded runner download" - a proxy 5xx or a sign-in page is not a
  source build;
* a download over plain HTTP is flagged: its ``.sha256`` rides the same
  unauthenticated connection as the binary;
* the archive's release signature (``.minisig``) is verified against
  the release key the scripts carry: a signature that does not verify, uses
  the legacy algorithm or was made for another archive is refused whatever
  the flag; one that could not be checked (none published, a body that is not
  a signature, a trusted comment that names no file, no verifier on the host)
  is a loud warning, and ``--require-signature`` /
  ``XTM_RUNNER_REQUIRE_SIGNATURE=1`` refuses it. Every verifier on every path
  reaches the same verdict on the same bytes. The
  signatures here are minted with throwaway keys handed to the scripts through
  ``XTM_RUNNER_MINISIGN_PUBKEY``, one of them by ``tauri signer sign`` itself.

POSIX only: install.sh refuses any OS but macOS and Linux, and install.ps1
runs under PowerShell 7 on the CI host (see ``needs_pwsh``). Every run stages
and installs under a path with a space in it.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

DIST_DIR = Path(__file__).resolve().parents[2] / "apps" / "runner" / "dist"
INSTALL_SH = DIST_DIR / "install.sh"
INSTALL_PS1 = DIST_DIR / "install.ps1"
ARCHIVE = "xtm-one-runner-linux-x64.tar.gz"
WIN_ARCHIVE = "xtm-one-runner-windows-x64.zip"
PROBE_PATH = "/api/v1/runners/download/linux/x64"
MANIFEST_PATH = "/api/v1/runners/download/manifest"
DIST_PREFIX = "/api/v1/runners/dist/"
MIRROR_PREFIX = "/mirror/"
SIGN_IN_PAGE = b"<!DOCTYPE html><html><body>Sign in to continue</body></html>\n"

BASH = shutil.which("bash")
PWSH = shutil.which("pwsh")
# Never on Windows: the scripts refuse any OS but macOS / Linux (install.sh),
# and install.ps1 stops a running xtm-one-runner process before replacing it,
# which on a developer's machine would be their real runner.
needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None or shutil.which("curl") is None,
    reason="install.sh runs on macOS / Linux with bash and curl",
)
needs_pwsh = pytest.mark.skipif(
    sys.platform == "win32" or PWSH is None,
    reason="install.ps1 is exercised with PowerShell 7 on a POSIX CI host",
)

# One worker for the whole module: install.ps1 stops EVERY process named
# xtm-one-runner on the host, and the install.sh tests run stand-in runners by
# that name, so under pytest-xdist a ps1 test on one worker SIGKILLs another
# worker's stand-in mid-check. The group requires `--dist loadgroup` (CI and
# scripts/ci-local.sh pass it).
pytestmark = pytest.mark.xdist_group("runner_install_scripts")

# Every external tool the script needs on the path it takes in these tests
# (download, read the key override, verify, extract, install). sha256sum /
# shasum are deliberately absent from this list: the restricted PATH is how a
# host without a hashing tool is simulated.
_TOOLS = (
    "curl",
    "awk",
    "tar",
    "gzip",
    "mktemp",
    "rm",
    "mkdir",
    "mv",
    "chmod",
    "sed",
    "tr",
    "od",
    "head",
    "wc",
    "base64",
)

# The .sha256 bodies a mirror may serve. "valid" is what the release writes
# (sha256sum); the next three are the other forms the self-updater accepts
# (apps/runner/src/update.rs: first token, case-insensitive).
_SHA_BODIES = {
    "valid": "{hex}  {name}\n",
    "bare": "{hex}\n",
    "crlf-upper": "{hex_upper}\r\n",
    "binary-crlf": "{hex} *{name}\r\n",
    "wrong": "{zero}  {name}\n",
    "empty": "",
    "html": SIGN_IN_PAGE.decode(),
}


def _archive_bytes() -> bytes:
    payload = b"#!/bin/sh\necho fake-runner\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("xtm-one-runner")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _sha_body(name: str, data: bytes, sha: str) -> bytes:
    digest = hashlib.sha256(data).hexdigest()
    return _SHA_BODIES[sha].format(hex=digest, hex_upper=digest.upper(), name=name, zero="0" * 64).encode()


class _SigningKey:
    """A throwaway minisign key: signs like ``tauri signer sign`` / minisign do.

    Derived from a label rather than drawn at random, so every run signs the
    same bytes.
    """

    def __init__(self, label: str) -> None:
        self._key = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label.encode()).digest())
        self.key_id = hashlib.sha256(f"key id of {label}".encode()).digest()[:8]
        raw = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        # The key line of a minisign .pub file: "Ed" + key id + key.
        self.pubkey = base64.b64encode(b"Ed" + self.key_id + raw).decode()

    def sign(self, data: bytes, *, name: str, prehashed: bool = True, trusted: str | None = None) -> str:
        """minisign's four-line text over ``data`` (``prehashed``: the "ED" algorithm)."""
        message = hashlib.blake2b(data, digest_size=64).digest() if prehashed else data
        signature = self._key.sign(message)
        comment = trusted if trusted is not None else f"timestamp:1790000000\tfile:{name}"
        global_signature = self._key.sign(signature + comment.encode())
        algorithm = b"ED" if prehashed else b"Ed"
        return (
            "untrusted comment: signature from a test key\n"
            + base64.b64encode(algorithm + self.key_id + signature).decode()
            + "\n"
            + f"trusted comment: {comment}\n"
            + base64.b64encode(global_signature).decode()
            + "\n"
        )


def _tauri_form(text: str) -> str:
    """How ``tauri signer sign`` writes a signature: base64 of the text, one line."""
    return base64.b64encode(text.encode()).decode()


_B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


# The key every run trusts (XTM_RUNNER_MINISIGN_PUBKEY), and one it does not.
KEY = _SigningKey("install-script test signer")
OTHER_KEY = _SigningKey("another install-script test signer")
# Trusted by no run: the key of a release that is not this one.
THIRD_KEY = _SigningKey("a third install-script test signer")

# Signed by the release tool itself: tauri-cli 2.11.4 ``tauri signer sign``,
# with a throwaway key whose private half was deleted, over archives kept as
# bytes (a signature covers exact bytes, and gzip output differs between zlib
# builds). TAURI_SIGNER is that signer's PUBLIC half in the form
# tauri.conf.json carries one: base64 of the whole .pub file. Each signature is
# as ``tauri signer sign`` writes it (base64 of minisign's text, one line).
TAURI_SIGNER = (
    "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDcwRTkxRTBDOTNEMjY0MjMKUldRalpOS1REQjdwY0VSVXVCeVd0enRn"
    "MVUzVWVOQ0k1RTU2MTdENGIrd3B2NUFRM0VpREREQUQK"
)
# The key ID the signer printed in that .pub file ("minisign public key: <ID>").
_TAURI_SIGNER_ID = base64.b64decode(TAURI_SIGNER).decode().splitlines()[0].rsplit(" ", 1)[1]
TAURI_SIGNED = {
    ARCHIVE: (
        "H4sIAAAAAAAC/+3SuwrCQBCF4anzFBHrJSO68XmiLmYLJ7AX8PFdRRDsFcH/a85wmtPMtVzcYsGlahaSfIQ2e+8f2byn6m77uu/9Rv04Sq/y"
        "BTWXKbV5+U/r1XCINuS5C8d56ctUU3Q5ni2cnj/RCQAAAAAAAAAAAAAAAADg99wAtdodbwAoAAA=",
        "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVRalpOS1REQjdwY01JaDAzakE4RmVQREtNajRD"
        "b3ZDK3Zoc080QXRmVnR3a0tEcmJqK2d6WC9QRGJ3TFVQVzFTNnJEK1pjTENtQ2EyRkJjdmNlZmV2Y0daYnNpTldDYVEwPQp0cnVzdGVkIGNv"
        "bW1lbnQ6IHRpbWVzdGFtcDoxNzkwNTEzNzUyCWZpbGU6eHRtLW9uZS1ydW5uZXItbGludXgteDY0LnRhci5negowLzdNZ1hJV21MZ01aM05y"
        "czV5Y3ppNkI4Ym5JeTgwY3VRMGdqMmNNSlNOL1ZwenkzUDdWdEZJN0tiOE90RDZOTTgyTlQrQ2R2bWFYbjNMNnBRSGdEUT09Cg==",
    ),
    WIN_ARCHIVE: (
        "UEsDBBQAAAAAAAAAIQBAHOtlEwAAABMAAAASAAAAeHRtLW9uZS1ydW5uZXIuZXhldGF1cmktc2lnbmVkLXJ1bm5lclBLAQIUABQAAAAAAAAA"
        "IQBAHOtlEwAAABMAAAASAAAAAAAAAAAAAACAAQAAAAB4dG0tb25lLXJ1bm5lci5leGVQSwUGAAAAAAEAAQBAAAAAQwAAAAAA",
        "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVRalpOS1REQjdwY0hoTjJYa2RDNitrU3NsQ01P"
        "UElBNDJaSkFGZHUzaHBjMmQ2bFBIWEx2Z2szcG1aZmtBMXVidy9ReUp2a1RnZVFKNW4vUVZ2RlVvV0crdWZSWmJaK0FzPQp0cnVzdGVkIGNv"
        "bW1lbnQ6IHRpbWVzdGFtcDoxNzkwNTEzNzUyCWZpbGU6eHRtLW9uZS1ydW5uZXItd2luZG93cy14NjQuemlwCnpvTUVGS0FFVEdhR0tiOXlE"
        "MWU4blFEeGpmZnRISFFRWTFpNDBjZkhSZnZZZEdsRGpnZTBqdG1XSGV5amlVSEJiVVkyOFNUYk9DamtGZE90NTNtZ0R3PT0K",
    ),
}


def _sig_body(name: str, data: bytes, sig: str) -> bytes:
    """The .minisig bodies a mirror may serve, good and bad."""
    if sig == "valid":
        return KEY.sign(data, name=name).encode()
    if sig == "tauri":
        return _tauri_form(KEY.sign(data, name=name)).encode()
    if sig == "legacy":
        return KEY.sign(data, name=name, prehashed=False).encode()
    if sig == "crlf":
        return KEY.sign(data, name=name).replace("\n", "\r\n").encode()
    if sig == "bom":
        return b"\xef\xbb\xbf" + KEY.sign(data, name=name).encode()
    if sig == "other-archive":
        # A valid signature of the same bytes, made for the arm64 build.
        return KEY.sign(data, name=name.replace("x64", "arm64")).encode()
    if sig == "extra-line":
        return (KEY.sign(data, name=name) + "one line too many\n").encode()
    if sig == "not-base64":
        # The signature line with characters base64 has not: OpenSSL 3.0
        # decodes the valid part and stops.
        lines = KEY.sign(data, name=name).splitlines()
        lines[1] += "!!"
        return ("\n".join(lines) + "\n").encode()
    if sig == "unpadded":
        # An incomplete last group: OpenSSL 3.5 drops it, GNU base64 decodes it.
        lines = KEY.sign(data, name=name).splitlines()
        lines[1] = lines[1].rstrip("=")
        return ("\n".join(lines) + "\n").encode()
    if sig == "wrapped-junk":
        return (_tauri_form(KEY.sign(data, name=name)) + "!!").encode()
    # What a mirror may do to a signature without changing a signed byte.
    if sig == "spaced":
        lines = KEY.sign(data, name=name).splitlines()
        lines[1] = lines[1][:50] + " " + lines[1][50:] + " "
        lines[3] = lines[3][:40] + "\t" + lines[3][40:]
        return ("\n".join(lines) + "\n").encode()
    if sig == "loose-bits":
        # The last character of the signature line with the bits it does not
        # use set: only the runner's minisign-verify refuses it.
        lines = KEY.sign(data, name=name).splitlines()
        data_chars = lines[1].rstrip("=")
        last = _B64_ALPHABET[_B64_ALPHABET.index(data_chars[-1]) | 1]
        lines[1] = data_chars[:-1] + last + lines[1][len(data_chars) :]
        return ("\n".join(lines) + "\n").encode()
    if sig == "indented":
        return ("  " + KEY.sign(data, name=name)).encode()
    if sig == "bare-comment":
        return KEY.sign(data, name=name).replace("untrusted comment: ", "untrusted comment:", 1).encode()
    if sig == "no-file":
        # A trusted comment of the signer's own (minisign -t).
        return KEY.sign(data, name=name, trusted="timestamp:1790000000\trelease build").encode()
    if sig == "tampered":
        return KEY.sign(data + b"x", name=name).encode()
    if sig == "other-key":
        return OTHER_KEY.sign(data, name=name).encode()
    if sig == "bad-comment":
        lines = KEY.sign(data, name=name).splitlines()
        lines[2] += " (edited)"
        return ("\n".join(lines) + "\n").encode()
    if sig == "html":
        return SIGN_IN_PAGE
    if sig == "empty":
        return b""
    raise AssertionError(sig)


class _Dist:
    """What the fake platform / mirror serves, and what it was asked for."""

    def __init__(self) -> None:
        self.base = ""
        self.files: dict[str, bytes] = {}
        # A status answered for one file instead of its bytes (a 503 .sha256).
        self.statuses: dict[str, int] = {}
        # Content-Type per file; a .sha256 defaults to what the platform's
        # FileResponse sends.
        self.content_types: dict[str, str] = {}
        # Files served with no Content-Length, read until the connection closes.
        self.unsized: set[str] = set()
        # Any other path answered 200 with these bytes (a proxy's sign-in page).
        self.pages: dict[str, bytes] = {}
        self.probe_status: int | None = None  # None -> redirect to the archive
        self.probe_location = DIST_PREFIX + ARCHIVE
        # install.ps1 reads the download manifest instead of probing.
        self.manifest_status: int | None = None  # None -> a manifest naming the zip
        self.manifest_sha256 = True
        self.manifest_signature = True
        self.manifest_artifacts: dict | None = None  # None -> the Windows zip only
        self.manifest_body: bytes | None = None  # served as text/html instead
        # The runner version the platform ships (the manifest's runner_version).
        self.manifest_runner_version: str | None = None
        self.requests: list[str] = []


def _handler(dist: _Dist) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _respond(self, send_body: bool) -> None:
            path = self.path.split("?", 1)[0]
            dist.requests.append(f"{self.command} {path}")
            if path in dist.pages:
                self._body(dist.pages[path], "text/html; charset=utf-8", send_body)
                return
            if path == MANIFEST_PATH:
                if dist.manifest_status is not None:
                    self._status(dist.manifest_status)
                    return
                if dist.manifest_body is not None:
                    self._body(dist.manifest_body, "text/html; charset=utf-8", send_body)
                    return
                artifacts = dist.manifest_artifacts
                if artifacts is None:
                    entry = {"url": dist.base + DIST_PREFIX + WIN_ARCHIVE}
                    if dist.manifest_sha256:
                        entry["sha256_url"] = dist.base + DIST_PREFIX + WIN_ARCHIVE + ".sha256"
                    if dist.manifest_signature:
                        entry["signature_url"] = dist.base + DIST_PREFIX + WIN_ARCHIVE + ".minisig"
                    artifacts = {"windows": {"x64": entry}}
                manifest: dict = {"version": "1.0.0", "artifacts": artifacts}
                if dist.manifest_runner_version is not None:
                    manifest["runner_version"] = dist.manifest_runner_version
                body = json.dumps(manifest).encode()
                self._body(body, "application/json", send_body)
                return
            if path == PROBE_PATH:
                if dist.probe_status is not None:
                    self._status(dist.probe_status)
                    return
                self.send_response(302)
                self.send_header("Location", dist.probe_location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            for prefix in (DIST_PREFIX, MIRROR_PREFIX):
                if path.startswith(prefix):
                    name = path[len(prefix) :]
                    if name in dist.statuses:
                        self._status(dist.statuses[name])
                        return
                    body = dist.files.get(name)
                    if body is None:
                        break
                    default_type = (
                        "text/plain; charset=utf-8" if name.endswith(".sha256") else "application/octet-stream"
                    )
                    content_type = dist.content_types.get(name, default_type)
                    self._body(body, content_type, send_body, sized=name not in dist.unsized)
                    return
            self._status(404)

        def _body(self, body: bytes, content_type: str, send_body: bool, *, sized: bool = True) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if sized:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def _status(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            self._respond(send_body=True)

        def do_HEAD(self) -> None:
            self._respond(send_body=False)

        def log_message(self, *_args) -> None:  # keep test output clean
            return

    return Handler


@pytest.fixture
def dist():
    state = _Dist()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _stage(dist: _Dist, *, sha: str | None = "valid", sig: str | None = None) -> None:
    archive = _archive_bytes()
    dist.files[ARCHIVE] = archive
    if sha is not None:
        dist.files[ARCHIVE + ".sha256"] = _sha_body(ARCHIVE, archive, sha)
    if sig is not None:
        dist.files[ARCHIVE + ".minisig"] = _sig_body(ARCHIVE, archive, sig)


def _root(tmp_path: Path) -> Path:
    """Where a run stages and installs: every quote in the scripts must survive the space."""
    root = tmp_path / "install root"
    root.mkdir(exist_ok=True)
    return root


def _fake_uname(bin_dir: Path) -> None:
    uname = bin_dir / "uname"
    uname.write_text(
        '#!/bin/sh\ncase "$1" in -m) echo x86_64 ;; *) echo Linux ;; esac\n',
        encoding="utf-8",
    )
    uname.chmod(0o755)


def _fake_recorder(bin_dir: Path, name: str) -> None:
    """A stand-in for a service manager: it records its arguments and succeeds."""
    tool = bin_dir / name
    tool.write_text(f'#!/bin/sh\necho "{name} $*" >> "$HOME/calls.log"\n', encoding="utf-8")
    tool.chmod(0o755)


def _run(
    tmp_path: Path,
    *args: str,
    hash_tool: bool = True,
    verifier: bool = True,
    autostart: bool = False,
    extra_env: dict[str, str] | None = None,
    script: Path = INSTALL_SH,
) -> subprocess.CompletedProcess[str]:
    root = _root(tmp_path)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    _fake_uname(bin_dir)
    if autostart:
        _fake_recorder(bin_dir, "systemctl")
        _fake_recorder(bin_dir, "loginctl")
    if hash_tool and verifier:
        path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    else:
        # A restricted PATH: no openssl / minisign (the signature verifiers),
        # and without hash_tool no sha256sum / shasum either.
        tools = _TOOLS if not hash_tool else (*_TOOLS, "sha256sum")
        for tool in tools:
            real = shutil.which(tool)
            if real is None:
                pytest.skip(f"{tool} is not available")
            link = bin_dir / tool
            if not link.exists():
                link.symlink_to(real)
        path = str(bin_dir)
    home = root / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": path,
        "HOME": str(home),
        "TMPDIR": str(root),
        # A display-less CI box would otherwise pick the headless archive.
        "XTM_RUNNER_FLAVOR": "desktop",
        "XTM_RUNNER_MINISIGN_PUBKEY": KEY.pubkey,
        **(extra_env or {}),
    }
    argv = [BASH, str(script), "--install-dir", str(root / "bin")]
    if not autostart:
        argv.append("--no-autostart")
    return subprocess.run(  # noqa: S603
        [*argv, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _installed(tmp_path: Path) -> bool:
    return (_root(tmp_path) / "bin" / "xtm-one-runner").is_file()


def _mirror(dist: _Dist) -> str:
    return dist.base + "/mirror"


# -- Integrity verification -------------------------------------------


@needs_bash
def test_verified_install_reports_sha256_verified(tmp_path, dist):
    _stage(dist)
    result = _run(tmp_path, "--download-base", _mirror(dist))
    out = _output(result)
    assert result.returncode == 0, out
    assert "SHA-256 verified." in out
    assert "WITHOUT integrity verification" not in out
    assert _installed(tmp_path)


@needs_bash
@pytest.mark.parametrize("sha", ["valid", "bare", "crlf-upper", "binary-crlf"])
def test_every_sha256_form_the_self_updater_reads_verifies(tmp_path, dist, sha):
    _stage(dist, sha=sha)
    result = _run(tmp_path, "--download-base", _mirror(dist), "--require-checksum")
    out = _output(result)
    assert result.returncode == 0, out
    assert "SHA-256 verified." in out
    assert _installed(tmp_path)


@needs_bash
def test_a_staging_dir_with_a_backslash_still_verifies(tmp_path, dist):
    # sha256sum escapes its output line when the hashed file's NAME holds a
    # backslash, which a path-based hash would read as a mismatch.
    _stage(dist)
    staging = tmp_path / "back\\slash"
    staging.mkdir()
    result = _run(tmp_path, "--download-base", _mirror(dist), extra_env={"TMPDIR": str(staging)})
    out = _output(result)
    assert result.returncode == 0, out
    assert "SHA-256 verified." in out


@needs_bash
def test_checksum_mismatch_still_refuses(tmp_path, dist):
    _stage(dist, sha="wrong")
    result = _run(tmp_path, "--download-base", _mirror(dist))
    assert result.returncode != 0
    assert "SHA-256 mismatch" in _output(result)
    assert not _installed(tmp_path)


@needs_bash
@pytest.mark.parametrize("flag", [(), ("--require-checksum",)], ids=["default", "require"])
def test_a_sha256_without_a_digest_is_refused_whatever_the_flag(tmp_path, dist, flag):
    _stage(dist, sha="html")
    result = _run(tmp_path, "--download-base", _mirror(dist), *flag)
    out = _output(result)
    assert result.returncode != 0, out
    assert f"the .sha256 at {_mirror(dist)}/{ARCHIVE}.sha256 does not hold a SHA-256 digest" in out
    assert "SHA-256 mismatch" not in out
    assert not _installed(tmp_path)


@needs_bash
def test_a_missing_sha256_is_a_loud_warning_that_names_the_cause(tmp_path, dist):
    _stage(dist, sha=None)
    result = _run(tmp_path, "--download-base", _mirror(dist))
    out = _output(result)
    assert result.returncode == 0, out
    assert "INTEGRITY NOT VERIFIED: no .sha256 could be fetched from" in out
    assert "WITHOUT integrity verification" in out
    assert "XTM One runner installed." not in out
    assert _installed(tmp_path)


@needs_bash
@pytest.mark.parametrize("status", [404, 503])
def test_an_unfetchable_sha256_names_its_status(tmp_path, dist, status):
    # curl -f fails on a 404 and on a 5xx alike: the status is what tells a
    # mirror without the file from a backend that is down.
    _stage(dist, sha=None)
    dist.statuses[ARCHIVE + ".sha256"] = status
    result = _run(tmp_path, "--download-base", _mirror(dist))
    out = _output(result)
    assert result.returncode == 0, out
    assert f"no .sha256 could be fetched from {_mirror(dist)}/{ARCHIVE}.sha256 (HTTP {status})" in out


@needs_bash
@pytest.mark.parametrize(
    ("flag", "expect_installed"),
    [((), True), (("--require-checksum",), False)],
    ids=["default", "require"],
)
def test_an_empty_sha256_is_named(tmp_path, dist, flag, expect_installed):
    _stage(dist, sha="empty")
    result = _run(tmp_path, "--download-base", _mirror(dist), *flag)
    out = _output(result)
    assert f"the .sha256 at {_mirror(dist)}/{ARCHIVE}.sha256 is empty" in out
    assert (result.returncode == 0) is expect_installed, out
    assert _installed(tmp_path) is expect_installed


@needs_bash
@pytest.mark.parametrize(
    ("flag", "env"),
    [
        (("--require-checksum",), None),
        ((), {"XTM_RUNNER_REQUIRE_CHECKSUM": "1"}),
        ((), {"XTM_RUNNER_REQUIRE_CHECKSUM": "true"}),
        ((), {"XTM_RUNNER_REQUIRE_CHECKSUM": "True"}),
        ((), {"XTM_RUNNER_REQUIRE_CHECKSUM": "YES"}),
    ],
    ids=["flag", "env-1", "env-true", "env-True", "env-YES"],
)
def test_require_checksum_refuses_a_missing_sha256(tmp_path, dist, flag, env):
    _stage(dist, sha=None)
    result = _run(tmp_path, "--download-base", _mirror(dist), *flag, extra_env=env)
    out = _output(result)
    assert result.returncode != 0, out
    assert "Refusing to install an unverified binary: no .sha256 could be fetched" in out
    assert not _installed(tmp_path)


@needs_bash
@pytest.mark.parametrize("value", ["0", "", "no"])
def test_without_the_opt_in_an_unverified_install_proceeds(tmp_path, dist, value):
    _stage(dist, sha=None)
    result = _run(tmp_path, "--download-base", _mirror(dist), extra_env={"XTM_RUNNER_REQUIRE_CHECKSUM": value})
    out = _output(result)
    assert result.returncode == 0, out
    assert "INTEGRITY NOT VERIFIED" in out
    assert _installed(tmp_path)


@needs_bash
def test_a_host_without_a_hashing_tool_is_named(tmp_path, dist):
    _stage(dist)
    result = _run(tmp_path, "--download-base", _mirror(dist), hash_tool=False)
    out = _output(result)
    assert result.returncode == 0, out
    assert "INTEGRITY NOT VERIFIED: no sha256sum or shasum on this host" in out
    assert _installed(tmp_path)


@needs_bash
def test_require_checksum_refuses_without_a_hashing_tool_before_downloading(tmp_path, dist):
    _stage(dist)
    result = _run(tmp_path, "--download-base", _mirror(dist), "--require-checksum", hash_tool=False)
    out = _output(result)
    assert result.returncode != 0, out
    assert "Refusing to install an unverified binary: no sha256sum or shasum on this host" in out
    assert not any(ARCHIVE in r for r in dist.requests)
    assert not _installed(tmp_path)


@needs_bash
def test_a_plain_http_download_is_flagged(tmp_path, dist):
    _stage(dist)
    result = _run(tmp_path, "--download-base", _mirror(dist))
    out = _output(result)
    assert result.returncode == 0, out
    assert f"Plain HTTP download ({_mirror(dist)}/{ARCHIVE})" in out
    assert "SHA-256 verified." in out


@needs_bash
def test_help_lists_the_opt_in(tmp_path):
    result = _run(tmp_path, "--help")
    assert result.returncode == 0, _output(result)
    assert "--require-checksum" in result.stdout
    assert "XTM_RUNNER_REQUIRE_CHECKSUM=1" in result.stdout


# -- Platform probe diagnosis -----------------------------------------


@needs_bash
def test_a_resolved_probe_installs_the_version_matched_archive(tmp_path, dist):
    _stage(dist)
    result = _run(tmp_path, "--server", dist.base)
    out = _output(result)
    assert result.returncode == 0, out
    assert "Version-matched to the platform" in out
    assert "SHA-256 verified." in out
    assert "not the runner archive" not in out


@needs_bash
@pytest.mark.parametrize(
    ("status", "phrase"),
    [
        (503, "answered HTTP 503: it is reachable but not serving downloads right now"),
        (502, "answered HTTP 502: it is reachable but not serving downloads right now"),
        (403, "refused the download request (HTTP 403): an auth proxy or WAF"),
        (401, "refused the download request (HTTP 401): an auth proxy or WAF"),
        (418, "Unexpected HTTP 418"),
    ],
)
def test_a_failed_probe_names_its_status_and_is_not_a_source_build(tmp_path, dist, status, phrase):
    dist.probe_status = status
    result = _run(tmp_path, "--server", dist.base)
    out = _output(result)
    assert result.returncode != 0
    assert phrase in out
    assert "source/dev build" not in out
    assert f"last probe: HTTP {status} from {dist.base}" in out


@needs_bash
def test_a_404_probe_keeps_the_source_build_hint(tmp_path, dist):
    # The platform 404s a source/dev build, runners disabled, and a build this
    # release does not ship alike (backend/app/api/v1/runners.py).
    dist.probe_status = 404
    result = _run(tmp_path, "--server", dist.base)
    out = _output(result)
    assert result.returncode != 0
    assert (
        "no embedded runner download for linux/x64 (HTTP 404: a source/dev build, runners disabled on this "
        "instance, or a release that does not ship this build)"
    ) in out
    assert "last probe: HTTP 404" in out


@needs_bash
def test_a_404_for_the_headless_build_names_the_flavor(tmp_path, dist):
    result = _run(tmp_path, "--server", dist.base, extra_env={"XTM_RUNNER_FLAVOR": "headless"})
    out = _output(result)
    assert result.returncode != 0
    assert "no embedded runner download for linux/x64 headless (HTTP 404" in out
    assert "HEAD /api/v1/runners/download/linux/headless" in dist.requests


@needs_bash
def test_a_probe_that_lands_on_a_sign_in_page_is_flagged(tmp_path, dist):
    # An auth proxy in front of the platform answers 200 too - with its own page.
    _stage(dist)
    dist.probe_location = "/sign-in?next=/api/v1/runners/download/linux/x64"
    dist.pages["/sign-in"] = SIGN_IN_PAGE
    result = _run(tmp_path, "--server", dist.base)
    out = _output(result)
    assert result.returncode != 0
    assert f"resolved the download to {dist.base}/sign-in?next=" in out
    assert "which is not the runner archive: an auth proxy or a sign-in page" in out
    assert not _installed(tmp_path)


@needs_bash
def test_an_unreachable_platform_is_reported_as_unreachable(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # The socket is closed: nothing listens on the port any more.
    result = _run(tmp_path, "--server", f"http://127.0.0.1:{port}")
    out = _output(result)
    assert result.returncode != 0
    assert "Could not reach http://127.0.0.1:" in out
    assert "last probe: HTTP 000" in out
    assert "source/dev build" not in out


@needs_bash
def test_a_failed_probe_falls_back_to_the_mirror(tmp_path, dist):
    _stage(dist)
    dist.probe_status = 503
    result = _run(tmp_path, "--server", dist.base, "--download-base", _mirror(dist))
    out = _output(result)
    assert result.returncode == 0, out
    assert "answered HTTP 503" in out
    assert "SHA-256 verified." in out


# -- Release signature ------------------------------------------------


RELEASE_SIGNING = DIST_DIR.parent / "release-signing.json"


def _release_trust() -> dict:
    """apps/runner/release-signing.json: the release keys and their signature floor."""
    return json.loads(RELEASE_SIGNING.read_text(encoding="utf-8"))


def _tauri_updater_key_line() -> str:
    """The key line of the bundle updater's key (tauri.conf.json plugins.updater.pubkey)."""
    conf = json.loads((DIST_DIR.parent / "tauri.conf.json").read_text(encoding="utf-8"))
    text = base64.b64decode(conf["plugins"]["updater"]["pubkey"]).decode()
    return next(line for line in text.splitlines() if line and not line.startswith("untrusted comment:"))


@pytest.mark.parametrize(
    ("script", "keys", "floor"),
    [
        (INSTALL_SH, r'^RELEASE_PUBKEYS="([A-Za-z0-9+/= ]+)"$', r"^RELEASE_TIMESTAMP_FLOOR=([0-9]+)$"),
        (INSTALL_PS1, r"^\$releasePubkeys = '([A-Za-z0-9+/= ]+)'\r?$", r"^\$releaseTimestampFloor = ([0-9]+)\r?$"),
    ],
    ids=["install.sh", "install.ps1"],
)
def test_the_scripts_carry_the_keys_and_the_floor_of_the_release(script, keys, floor):
    # The scripts are the trust root of a first install: a key added to
    # release-signing.json without them would refuse every release it signs.
    text = script.read_text(encoding="utf-8")
    key_match = re.search(keys, text, re.MULTILINE)
    floor_match = re.search(floor, text, re.MULTILINE)
    assert key_match, f"no release keys found in {script.name}"
    assert floor_match, f"no signature floor found in {script.name}"
    trust = _release_trust()
    assert key_match.group(1).split(" ") == trust["keys"]
    assert int(floor_match.group(1)) == trust["timestamp_floor"]


def test_the_release_trusts_the_key_the_bundle_updater_is_built_with():
    # The Tauri bundle updater takes one key (plugins.updater.pubkey): it must be
    # one of the release keys, or the release signs what half the fleet refuses.
    trust = _release_trust()
    assert _tauri_updater_key_line() in trust["keys"]
    assert EMBED.release_trust(RELEASE_SIGNING) == (trust["keys"], trust["timestamp_floor"])


def test_the_release_build_compiles_the_trust_file_into_the_runner_and_signs_with_the_version():
    # One source for the keys: release-compile.sh bakes release-signing.json
    # into both flavors, and release-stage.sh signs the bare archives with the
    # runner version it is given. The release workflow (XTM-One-Platform/release)
    # runs both from the commit it releases.
    scripts = DIST_DIR.parent / "scripts"
    compile_ = (scripts / "release-compile.sh").read_text(encoding="utf-8")
    stage = (scripts / "release-stage.sh").read_text(encoding="utf-8")
    assert compile_.count("""XTM_RUNNER_MINISIGN_PUBKEY="$(jq -r '.keys | join(",")' release-signing.json)\"""") == 1
    assert compile_.count("""XTM_RUNNER_SIGNATURE_FLOOR="$(jq -r '.timestamp_floor' release-signing.json)\"""") == 1
    # Exported before the flavor is picked: both builds carry the keys.
    assert compile_.index("export XTM_RUNNER_MINISIGN_PUBKEY") < compile_.index("case \"$flavor\" in")
    for text in (compile_, stage):
        assert "plugins.updater.pubkey' tauri.conf.json" not in text
        assert "tauri signer sign -k" not in text
    assert 'node scripts/sign-release-archive.mjs "$1" "$version"' in stage
    assert '--expect-file "$(basename "$1")" --expect-version "$version" "$1"' in stage


def test_the_self_updater_expects_the_archive_names_the_platform_serves():
    # update.rs requires the signed file: field to name the archive of its own
    # os / arch / flavor; its table must be the one the platform serves from.
    from app.api.v1.runners import _BARE_ARCHIVES, _HEADLESS_ARCHIVES

    source = (DIST_DIR.parent / "src" / "update.rs").read_text(encoding="utf-8")
    table = re.search(r"const SERVED_ARCHIVES: &\[\(&str, &str, &str, &str\)\] = &\[(.*?)\];", source, re.S)
    assert table, "SERVED_ARCHIVES is gone from update.rs"
    rows = re.findall(r'\(\s*"(\w+)",\s*"(\w+)",\s*"(\w+)",\s*"([^"]+)",?\s*\)', table.group(1))
    served = {(os_name, arch, "desktop"): name for (os_name, arch), name in _BARE_ARCHIVES.items()}
    served.update({(os_name, arch, "headless"): name for (os_name, arch), name in _HEADLESS_ARCHIVES.items()})
    assert {(os_name, arch, flavor): name for os_name, arch, flavor, name in rows} == served


# Every published form, and what a mirror may do to one without changing a
# signed byte: every verifier, on every path, verifies all of them.
_SIGNED_FORMS = ["valid", "tauri", "crlf", "bom", "spaced", "loose-bits", "indented", "bare-comment"]
# Bodies no verifier reads as a signature, whatever the host's base64 decoder.
_NOT_A_SIGNATURE = ["html", "extra-line", "not-base64", "unpadded", "wrapped-junk"]


@needs_bash
@pytest.mark.parametrize("sig", _SIGNED_FORMS)
def test_a_signed_archive_verifies(tmp_path, dist, sig):
    _stage(dist, sig=sig)
    result = _run(tmp_path, "--download-base", _mirror(dist), "--require-signature")
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified" in out
    assert "XTM One runner installed." in out
    assert _installed(tmp_path)


@needs_bash
def test_a_platform_install_verifies_the_signature_next_to_the_archive(tmp_path, dist):
    _stage(dist, sig="valid")
    result = _run(tmp_path, "--server", dist.base, "--require-signature")
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified" in out
    assert f"GET {DIST_PREFIX}{ARCHIVE}.minisig" in dist.requests


@needs_bash
@pytest.mark.parametrize("sig", ["tampered", "other-key", "bad-comment"])
@pytest.mark.parametrize("flag", [(), ("--require-signature",)], ids=["default", "require"])
def test_a_signature_that_does_not_verify_is_refused_whatever_the_flag(tmp_path, dist, sig, flag):
    _stage(dist, sig=sig)
    result = _run(tmp_path, "--download-base", _mirror(dist), *flag)
    out = _output(result)
    assert result.returncode != 0, out
    assert f"the signature at {_mirror(dist)}/{ARCHIVE}.minisig does not verify against the release key" in out
    assert not _installed(tmp_path)


# What the release never signs this way: the legacy algorithm (the self-updater
# refuses it too), a signature made for another build.
_NOT_THIS_RELEASE = [
    ("legacy", "the .minisig at {url} is a legacy (not prehashed) minisign signature, which the release never makes"),
    (
        "other-archive",
        "the signature at {url} is not for {archive} (its trusted comment names {other}) - a signed build of "
        "another platform or flavor was served in its place",
    ),
]


@needs_bash
@pytest.mark.parametrize(("sig", "phrase"), _NOT_THIS_RELEASE, ids=[s for s, _ in _NOT_THIS_RELEASE])
@pytest.mark.parametrize("flag", [(), ("--require-signature",)], ids=["default", "require"])
def test_a_signature_the_release_did_not_make_for_this_archive_is_refused(tmp_path, dist, sig, phrase, flag):
    _stage(dist, sig=sig)
    result = _run(tmp_path, "--download-base", _mirror(dist), *flag)
    out = _output(result)
    assert result.returncode != 0, out
    url = f"{_mirror(dist)}/{ARCHIVE}.minisig"
    assert phrase.format(url=url, archive=ARCHIVE, other=ARCHIVE.replace("x64", "arm64")) in out
    assert not _installed(tmp_path)


def _names_no_file(url: str, archive: str) -> str:
    return f"the trusted comment of the signature at {url} names no file, so the signature cannot be tied to {archive}"


@needs_bash
@pytest.mark.parametrize("flag", [(), ("--require-signature",)], ids=["default", "require"])
def test_a_signature_that_names_no_file_is_not_tied_to_the_archive(tmp_path, dist, flag):
    # Bound to no archive rather than to another one: nothing checks what it
    # was made for, as with no signature, and the opt-in refuses it.
    _stage(dist, sig="no-file")
    result = _run(tmp_path, "--download-base", _mirror(dist), *flag)
    out = _output(result)
    reason = _names_no_file(f"{_mirror(dist)}/{ARCHIVE}.minisig", ARCHIVE)
    assert "Release signature verified" not in out
    if flag:
        assert result.returncode != 0, out
        assert f"Refusing to install an unverified binary: {reason} (--require-signature)." in out
        assert not _installed(tmp_path)
    else:
        assert result.returncode == 0, out
        assert f"SIGNATURE NOT VERIFIED: {reason}" in out
        assert _installed(tmp_path)


@needs_bash
def test_a_tauri_cli_signature_verifies_with_the_key_as_tauri_conf_json_carries_it(tmp_path, dist):
    # The release's own format end to end: a .minisig as release-stage.sh stages it
    # (minisign's text, decoded from what tauri signer wrote), and the key in
    # the form the build bakes into the runner.
    archive, signature = TAURI_SIGNED[ARCHIVE]
    data = base64.b64decode(archive)
    dist.files[ARCHIVE] = data
    dist.files[ARCHIVE + ".sha256"] = _sha_body(ARCHIVE, data, "valid")
    dist.files[ARCHIVE + ".minisig"] = base64.b64decode(signature)
    result = _run(
        tmp_path,
        "--download-base",
        _mirror(dist),
        "--require-signature",
        extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": TAURI_SIGNER},
    )
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified" in out
    assert f"{_OVERRIDE_NOTICE} {_TAURI_SIGNER_ID}." in out
    assert _installed(tmp_path)


def _unusable_key(value: str) -> str:
    # "wrapped-junk": the form tauri.conf.json carries, then characters base64 has not.
    return _tauri_pubkey(KEY.pubkey) + "!!" if value == "wrapped-junk" else value


@needs_bash
@pytest.mark.parametrize("verifier", [True, False], ids=["verifier", "no-verifier"])
@pytest.mark.parametrize("value", ["not-a-key", "wrapped-junk"])
def test_an_unusable_key_override_is_refused_before_downloading(tmp_path, dist, verifier, value):
    # Even where nothing could check a signature: the override says one is expected.
    _stage(dist, sig="valid")
    result = _run(
        tmp_path,
        "--download-base",
        _mirror(dist),
        verifier=verifier,
        extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": _unusable_key(value)},
    )
    out = _output(result)
    assert result.returncode != 0, out
    assert "XTM_RUNNER_MINISIGN_PUBKEY is not a minisign public key" in out
    assert not any(ARCHIVE in r for r in dist.requests)
    assert not _installed(tmp_path)


# The order of the Ed25519 base point.
_L = 2**252 + 27742317777372353535851937790883648493

# Keys of small order, as the 32 bytes of a key line, with their order.
_SMALL_ORDER_KEYS = {
    "identity": (b"\x01" + b"\x00" * 31, 1),
    # A point of order 4 whose bytes od would print as one line and a "*".
    "all-zero": (b"\x00" * 32, 4),
}


def _small_order_forgery(name: str, data: bytes, point: bytes, order: int) -> tuple[str, bytes]:
    """A key of small order, and a .minisig it "verifies" for ``data``.

    With A of order n, [h]A is the identity whenever n divides h, so an R of
    known logarithm s satisfies [s]B = R + [h]A as soon as h = H(R || A || M)
    is a multiple of n: a few tries per signature. OpenSSL accepts both, the
    scripts must refuse a key of small order themselves.
    """
    key_id = b"smallord"

    def forge(message: bytes) -> bytes:
        for attempt in range(1000):
            seed = hashlib.sha256(b"small-order forgery %d" % attempt).digest()
            r = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            # R = [s]B for the scalar Ed25519 derives from the seed (RFC 8032 5.1.5).
            clamped = bytearray(hashlib.sha512(seed).digest()[:32])
            clamped[0] &= 248
            clamped[31] = (clamped[31] & 127) | 64
            s = int.from_bytes(clamped, "little") % _L
            if int.from_bytes(hashlib.sha512(r + point + message).digest(), "little") % _L % order == 0:
                return r + s.to_bytes(32, "little")
        raise AssertionError("no forgery found")

    comment = f"timestamp:1790000000\tfile:{name}"
    signature = forge(hashlib.blake2b(data, digest_size=64).digest())
    global_signature = forge(signature + comment.encode())
    key_line = base64.b64encode(b"Ed" + key_id + point).decode()
    text = (
        "untrusted comment: a forgery\n"
        + base64.b64encode(b"ED" + key_id + signature).decode()
        + f"\ntrusted comment: {comment}\n"
        + base64.b64encode(global_signature).decode()
        + "\n"
    )
    return key_line, text.encode()


@needs_bash
@pytest.mark.parametrize("key", list(_SMALL_ORDER_KEYS))
@pytest.mark.parametrize("flag", [(), ("--require-signature",)], ids=["default", "require"])
def test_a_key_of_small_order_is_refused_whatever_the_flag(tmp_path, dist, key, flag):
    _stage(dist)
    key_line, forged = _small_order_forgery(ARCHIVE, dist.files[ARCHIVE], *_SMALL_ORDER_KEYS[key])
    dist.files[ARCHIVE + ".minisig"] = forged
    result = _run(tmp_path, "--download-base", _mirror(dist), *flag, extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": key_line})
    out = _output(result)
    assert result.returncode != 0, out
    assert f"the signature at {_mirror(dist)}/{ARCHIVE}.minisig does not verify against the release key" in out
    assert not _installed(tmp_path)


# A key with an order-8 component (A = [a]B + T), an ordinary signature of the
# archive, and a global signature whose R is the order-8 point c7176a...037a:
# both satisfy the cofactorless equation, so OpenSSL accepts them, while
# libsodium (minisign) and install.ps1 refuse an R of small order. Made with a
# throwaway scalar that was never stored, over an archive kept as bytes.
MIXED_ORDER_SIGNED = (
    "RWRtaXhlZG9yZDLcacgSModa3cDkPS6IUL7TmbuwyWCH4fvqCWgUozbE",
    "H4sIAAAAAAAC/+3SsQqDQBCE4a19CsVa3HBc8jwaLijiCaeCj+8phIC9EvD/mlmmmWaXqS8G74owe++CnEKjl7V7RsdUNeZ3b/1D7dNKqnKB"
    "eZyqEOflnvKsrFtfjk3i3s2Qfqru+wuJAAAAAAAAAAAAAAAAAAD+1wowY/hJACgAAA==",
    "dW50cnVzdGVkIGNvbW1lbnQ6IGdsb2JhbCBzaWduYXR1cmUgd2l0aCBhbiBSIG9mIHNtYWxsIG9yZGVyClJVUnRhWGhsWkc5eVpHNDVTK0Vq"
    "bXJ6TFhUc0l2c1ZzbTNjellwc2ZzVXlqK3FQcnJoOFVkMUFwaVNKdUZWRHA3aTMrZHpHTjdXa0NVNUU5bGo2WS9CTmdLZjBubFdIaHdBdz0K"
    "dHJ1c3RlZCBjb21tZW50OiB0aW1lc3RhbXA6MTc5MDAwMDAwMwlmaWxlOnh0bS1vbmUtcnVubmVyLWxpbnV4LXg2NC50YXIuZ3oKeHhkcWNE"
    "MU4yRSs2UEF0MkRSQm5EeW9nVS9vc09jekdUc2Y5ZDVLc0EzcHFEdk5oSzEvdXo2eEVjOG45b2tPaDNuYW41aWQwQmQxZDRaMTcwZCtIQ3c9"
    "PQo=",
)


@needs_bash
@pytest.mark.parametrize("flag", [(), ("--require-signature",)], ids=["default", "require"])
def test_a_trusted_comment_signed_with_an_r_of_small_order_is_refused(tmp_path, dist, flag):
    key_line, archive, signature = MIXED_ORDER_SIGNED
    data = base64.b64decode(archive)
    dist.files[ARCHIVE] = data
    dist.files[ARCHIVE + ".sha256"] = _sha_body(ARCHIVE, data, "valid")
    dist.files[ARCHIVE + ".minisig"] = base64.b64decode(signature)
    result = _run(tmp_path, "--download-base", _mirror(dist), *flag, extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": key_line})
    out = _output(result)
    assert result.returncode != 0, out
    assert "does not verify against the release key (its trusted comment does not verify)" in out
    assert not _installed(tmp_path)


@needs_bash
@pytest.mark.parametrize("sig", ["valid", "other-archive"])
def test_a_trusted_comment_that_cannot_be_read_is_refused(tmp_path, dist, sig):
    # Naming the archive is the last check: an awk that fails on the program
    # reading the trusted comment must stop the install, not skip the check.
    real_awk = shutil.which("awk")
    if real_awk is None:
        pytest.skip("awk is not available")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "awk"
    fake.write_text(f'#!/bin/sh\ncase "$*" in *name=file:*) exit 2 ;; esac\nexec "{real_awk}" "$@"\n', encoding="utf-8")
    fake.chmod(0o755)
    _stage(dist, sig=sig)
    result = _run(tmp_path, "--download-base", _mirror(dist))
    out = _output(result)
    assert result.returncode != 0, out
    assert f"the trusted comment of the signature at {_mirror(dist)}/{ARCHIVE}.minisig could not be read" in out
    assert not _installed(tmp_path)


def _key_id(key_line: str) -> str:
    """A key line's key ID as minisign and the .pub file print it."""
    return format(int.from_bytes(base64.b64decode(key_line)[2:10], "little"), "X")


_OVERRIDE_NOTICE = (
    "XTM_RUNNER_MINISIGN_PUBKEY replaces the release keys this script carries: signatures are checked against key ID"
)


@needs_bash
def test_a_key_override_is_announced_with_its_key_id(tmp_path, dist):
    _stage(dist, sig="valid")
    result = _run(tmp_path, "--download-base", _mirror(dist))
    out = _output(result)
    assert result.returncode == 0, out
    assert f"{_OVERRIDE_NOTICE} {_key_id(KEY.pubkey)}." in out


@needs_bash
def test_the_release_key_is_not_announced_as_an_override(tmp_path, dist):
    _stage(dist)
    result = _run(tmp_path, "--download-base", _mirror(dist), extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""})
    out = _output(result)
    assert result.returncode == 0, out
    assert _OVERRIDE_NOTICE not in out


@needs_bash
def test_the_real_release_key_refuses_a_test_signature(tmp_path, dist):
    # Without the override the scripts trust the release key only.
    _stage(dist, sig="valid")
    result = _run(tmp_path, "--download-base", _mirror(dist), extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""})
    out = _output(result)
    assert result.returncode != 0, out
    assert "does not verify against the release key" in out
    assert not _installed(tmp_path)


@needs_bash
@pytest.mark.parametrize("sig", _NOT_A_SIGNATURE)
@pytest.mark.parametrize("flag", [(), ("--require-signature",)], ids=["default", "require"])
def test_a_minisig_that_is_not_a_signature_counts_as_a_missing_one(tmp_path, dist, sig, flag):
    # A mirror's error page or an SPA catch-all answering 200 must not end an
    # install that works without the check; the opt-in still refuses it.
    _stage(dist, sig=sig)
    result = _run(tmp_path, "--download-base", _mirror(dist), *flag)
    out = _output(result)
    reason = f"the .minisig at {_mirror(dist)}/{ARCHIVE}.minisig is not a minisign signature"
    if flag:
        assert result.returncode != 0, out
        assert f"Refusing to install an unverified binary: {reason} (--require-signature)." in out
        assert not _installed(tmp_path)
    else:
        assert result.returncode == 0, out
        assert f"SIGNATURE NOT VERIFIED: {reason}" in out
        assert _installed(tmp_path)


@needs_bash
@pytest.mark.parametrize(
    ("sig", "status", "phrase"),
    [
        (None, None, "no .minisig could be fetched from {url} (HTTP 404)"),
        (None, 503, "no .minisig could be fetched from {url} (HTTP 503)"),
        ("empty", None, "the .minisig at {url} is empty"),
    ],
    ids=["missing", "unavailable", "empty"],
)
def test_an_unchecked_signature_is_a_loud_warning(tmp_path, dist, sig, status, phrase):
    _stage(dist, sig=sig)
    if status is not None:
        dist.statuses[ARCHIVE + ".minisig"] = status
    result = _run(tmp_path, "--download-base", _mirror(dist))
    out = _output(result)
    assert result.returncode == 0, out
    reason = phrase.format(url=f"{_mirror(dist)}/{ARCHIVE}.minisig")
    assert f"SIGNATURE NOT VERIFIED: {reason}" in out
    assert "WITHOUT signature verification" in out
    assert "XTM One runner installed." not in out
    assert _installed(tmp_path)


@needs_bash
@pytest.mark.parametrize(
    ("flag", "env"),
    [
        (("--require-signature",), None),
        ((), {"XTM_RUNNER_REQUIRE_SIGNATURE": "1"}),
        ((), {"XTM_RUNNER_REQUIRE_SIGNATURE": "True"}),
    ],
    ids=["flag", "env-1", "env-True"],
)
def test_require_signature_refuses_an_unsigned_archive(tmp_path, dist, flag, env):
    _stage(dist)
    result = _run(tmp_path, "--download-base", _mirror(dist), *flag, extra_env=env)
    out = _output(result)
    assert result.returncode != 0, out
    assert "Refusing to install an unverified binary: no .minisig could be fetched" in out
    assert not _installed(tmp_path)


@needs_bash
def test_a_host_without_a_verifier_is_named(tmp_path, dist):
    _stage(dist, sig="valid")
    result = _run(tmp_path, "--download-base", _mirror(dist), verifier=False)
    out = _output(result)
    assert result.returncode == 0, out
    assert "SIGNATURE NOT VERIFIED: neither minisign nor OpenSSL 3" in out
    assert "SHA-256 verified." in out
    assert _installed(tmp_path)


@needs_bash
def test_require_signature_refuses_without_a_verifier_before_downloading(tmp_path, dist):
    _stage(dist, sig="valid")
    result = _run(tmp_path, "--download-base", _mirror(dist), "--require-signature", verifier=False)
    out = _output(result)
    assert result.returncode != 0, out
    assert "neither minisign nor OpenSSL 3 (Ed25519 + BLAKE2b) on this host can check" in out
    assert not any(ARCHIVE in r for r in dist.requests)
    assert not _installed(tmp_path)


@needs_bash
def test_a_verifier_that_accepts_anything_is_not_trusted(tmp_path, dist):
    # A minisign that says yes to everything fails the self-test (it accepts
    # the fixture over other bytes), so the tampered signature is still caught.
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "minisign"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    _stage(dist, sig="tampered")
    result = _run(tmp_path, "--download-base", _mirror(dist))
    out = _output(result)
    assert result.returncode != 0, out
    assert "does not verify against the release key" in out
    assert not _installed(tmp_path)


@needs_bash
def test_help_lists_the_signature_opt_in(tmp_path):
    result = _run(tmp_path, "--help")
    assert result.returncode == 0, _output(result)
    assert "--require-signature" in result.stdout
    assert "XTM_RUNNER_REQUIRE_SIGNATURE=1" in result.stdout


# -- Autostart -----------------------------------------------------------------


@needs_bash
def test_the_systemd_unit_survives_an_install_dir_with_spaces(tmp_path, dist):
    _stage(dist)
    result = _run(tmp_path, "--server", dist.base, "--key", "renr-test", autostart=True)
    out = _output(result)
    assert result.returncode == 0, out
    root = _root(tmp_path)
    unit = (root / "home" / ".config" / "systemd" / "user" / "xtm-one-runner.service").read_text(encoding="utf-8")
    assert f'ExecStart="{root / "bin" / "xtm-one-runner"}" run' in unit
    assert "systemctl --user enable --now xtm-one-runner.service" in (root / "home" / "calls.log").read_text(
        encoding="utf-8"
    )


# -- install.ps1: same two contracts ---------------------------


def _stage_windows(dist: _Dist, *, sha: str | None = "valid", sig: str | None = None) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xtm-one-runner.exe", b"fake-runner")
    data = buf.getvalue()
    dist.files[WIN_ARCHIVE] = data
    if sha is not None:
        dist.files[WIN_ARCHIVE + ".sha256"] = _sha_body(WIN_ARCHIVE, data, sha)
    if sig is not None:
        dist.files[WIN_ARCHIVE + ".minisig"] = _sig_body(WIN_ARCHIVE, data, sig)


def _ps1_argv(interpreter: str, *args: str, script_path: Path = INSTALL_PS1) -> list[str]:
    """Run install.ps1 and print a terminating error's message on one line.

    PowerShell's own error view wraps a message at the console width, which
    would split the phrases asserted below; the wrapper sidesteps it. Parameter
    names stay bare (a quoted ``'-Server'`` would bind as a positional string).
    """

    def token(arg: str) -> str:
        return arg if arg.startswith("-") else "'" + arg.replace("'", "''") + "'"

    script = str(script_path).replace("'", "''")
    call = " ".join(token(a) for a in args)
    command = f"try {{ & '{script}' {call} }} catch {{ [Console]::Error.WriteLine($_.Exception.Message); exit 1 }}"
    return [interpreter, "-NoProfile", "-NonInteractive", "-Command", command]


# The first PowerShell process on a cold CI host pays for the pwsh start and
# for loading the C# compiler that install.ps1's Add-Type needs (its minisign
# verifier, compiled on every run). On a 2-core runner shared with the other
# xdist worker that one-time cost alone ran past the 120 s bound of a single
# install (the first ps1 test of a worker timed out, the next took 57 s,
# every later one about 3 s). It is paid here once per worker, under its own
# bound, so the bound of each install measures that install alone.
_PWSH_WARM_UP_TIMEOUT = 300

# PowerShell calls out to the network around every script it runs: the update
# check queries the release feed at start and the telemetry is flushed at
# exit (the .NET host adds its own first-run banner and telemetry). On a CI
# host whose outbound call hangs, a one-line Add-Type waited past the 300 s
# warm-up bound where the next run of the same job took 8 s. These are the
# documented switches that turn those calls off; the tests need none of them.
_PWSH_QUIET_ENV = {
    "POWERSHELL_UPDATECHECK": "Off",
    "POWERSHELL_TELEMETRY_OPTOUT": "1",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_NOLOGO": "1",
}


def _partial_output(exc: subprocess.TimeoutExpired) -> str:
    """What a process that ran past its bound wrote before it was killed."""

    def text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        return value.decode(errors="replace") if isinstance(value, bytes) else value

    return f"stdout: {text(exc.stdout)!r}\nstderr: {text(exc.stderr)!r}"


@functools.cache
def _warm_up_pwsh() -> None:
    if PWSH is None:
        return
    command = (
        "Add-Type -TypeDefinition 'public static class XtmOneInstallTestWarmUp "
        "{ public static int One() { return 1; } }'; exit ([XtmOneInstallTestWarmUp]::One() - 1)"
    )
    try:
        result = subprocess.run(  # noqa: S603
            [PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
            env={**os.environ, **_PWSH_QUIET_ENV},
            capture_output=True,
            text=True,
            timeout=_PWSH_WARM_UP_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"PowerShell did not start and compile a type within {_PWSH_WARM_UP_TIMEOUT} s on this host\n"
            + _partial_output(exc)
        )
    assert result.returncode == 0, _output(result)


def _run_ps1(
    tmp_path: Path, *args: str, extra_env: dict[str, str] | None = None, script: Path = INSTALL_PS1
) -> subprocess.CompletedProcess[str]:
    _warm_up_pwsh()
    root = _root(tmp_path)
    home = root / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        # install.ps1 stages its download under $env:TEMP.
        "TEMP": str(root),
        "TMPDIR": str(root),
        "XTM_RUNNER_MINISIGN_PUBKEY": KEY.pubkey,
        **_PWSH_QUIET_ENV,
        **(extra_env or {}),
    }
    return subprocess.run(  # noqa: S603
        _ps1_argv(PWSH, "-InstallDir", str(root / "bin"), "-NoPathUpdate", *args, script_path=script),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _installed_exe(tmp_path: Path) -> bool:
    return (_root(tmp_path) / "bin" / "xtm-one-runner.exe").is_file()


@needs_pwsh
def test_ps1_verified_install_reports_sha256_verified(tmp_path, dist):
    _stage_windows(dist)
    result = _run_ps1(tmp_path, "-Server", dist.base)
    out = _output(result)
    assert result.returncode == 0, out
    assert "SHA-256 verified." in out
    assert _installed_exe(tmp_path)


@needs_pwsh
@pytest.mark.parametrize(
    ("sha", "content_type"),
    [
        ("bare", None),
        ("crlf-upper", None),
        ("binary-crlf", None),
        # A mirror serving it as bytes: PowerShell 7 hands back a byte[].
        ("valid", "application/octet-stream"),
    ],
)
def test_ps1_every_sha256_form_the_self_updater_reads_verifies(tmp_path, dist, sha, content_type):
    _stage_windows(dist, sha=sha)
    if content_type:
        dist.content_types[WIN_ARCHIVE + ".sha256"] = content_type
    result = _run_ps1(tmp_path, "-Server", dist.base, "-RequireChecksum")
    out = _output(result)
    assert result.returncode == 0, out
    assert "SHA-256 verified." in out
    assert _installed_exe(tmp_path)


@needs_pwsh
def test_ps1_a_mirror_install_verifies(tmp_path, dist):
    _stage_windows(dist)
    result = _run_ps1(tmp_path, "-DownloadBase", _mirror(dist))
    out = _output(result)
    assert result.returncode == 0, out
    assert "SHA-256 verified." in out
    assert f"Plain HTTP download ({_mirror(dist)}/{WIN_ARCHIVE})" in out
    assert _installed_exe(tmp_path)


@needs_pwsh
def test_ps1_checksum_mismatch_still_refuses(tmp_path, dist):
    _stage_windows(dist, sha="wrong")
    result = _run_ps1(tmp_path, "-Server", dist.base)
    out = _output(result)
    assert result.returncode != 0
    assert "SHA-256 mismatch" in out
    assert not _installed_exe(tmp_path)


@needs_pwsh
@pytest.mark.parametrize("flag", [(), ("-RequireChecksum",)], ids=["default", "switch"])
def test_ps1_a_sha256_without_a_digest_is_refused_whatever_the_switch(tmp_path, dist, flag):
    _stage_windows(dist, sha="html")
    result = _run_ps1(tmp_path, "-Server", dist.base, *flag)
    out = _output(result)
    assert result.returncode != 0, out
    assert f"the .sha256 at {dist.base}{DIST_PREFIX}{WIN_ARCHIVE}.sha256 does not hold a SHA-256 digest" in out
    assert not _installed_exe(tmp_path)


@needs_pwsh
def test_ps1_an_unfetchable_sha256_is_a_loud_warning(tmp_path, dist):
    _stage_windows(dist, sha=None)
    result = _run_ps1(tmp_path, "-Server", dist.base)
    out = _output(result)
    assert result.returncode == 0, out
    assert "INTEGRITY NOT VERIFIED: no .sha256 could be fetched from" in out
    assert "(HTTP 404)" in out
    assert "WITHOUT integrity verification" in out
    assert _installed_exe(tmp_path)


@needs_pwsh
@pytest.mark.parametrize(
    ("flag", "expect_installed"),
    [((), True), (("-RequireChecksum",), False)],
    ids=["default", "switch"],
)
def test_ps1_an_empty_sha256_is_named(tmp_path, dist, flag, expect_installed):
    _stage_windows(dist, sha="empty")
    result = _run_ps1(tmp_path, "-Server", dist.base, *flag)
    out = _output(result)
    assert "is empty" in out
    assert (result.returncode == 0) is expect_installed, out
    assert _installed_exe(tmp_path) is expect_installed


@needs_pwsh
def test_ps1_an_unpublished_sha256_is_named(tmp_path, dist):
    _stage_windows(dist)
    dist.manifest_sha256 = False
    result = _run_ps1(tmp_path, "-Server", dist.base)
    out = _output(result)
    assert result.returncode == 0, out
    assert "INTEGRITY NOT VERIFIED: no .sha256 is published next to the archive" in out


@needs_pwsh
@pytest.mark.parametrize(
    ("flag", "env"),
    [
        (("-RequireChecksum",), None),
        ((), {"XTM_RUNNER_REQUIRE_CHECKSUM": "1"}),
        ((), {"XTM_RUNNER_REQUIRE_CHECKSUM": "True"}),
    ],
    ids=["switch", "env-1", "env-True"],
)
def test_ps1_require_checksum_refuses_a_missing_sha256(tmp_path, dist, flag, env):
    _stage_windows(dist, sha=None)
    result = _run_ps1(tmp_path, "-Server", dist.base, *flag, extra_env=env)
    out = _output(result)
    assert result.returncode != 0, out
    assert "Refusing to install an unverified binary" in out
    assert not _installed_exe(tmp_path)


@needs_pwsh
def test_ps1_a_plain_http_manifest_is_flagged(tmp_path, dist):
    _stage_windows(dist)
    result = _run_ps1(tmp_path, "-Server", dist.base)
    out = _output(result)
    assert result.returncode == 0, out
    assert f"Plain HTTP download ({dist.base}{MANIFEST_PATH})" in out


@needs_pwsh
@pytest.mark.parametrize(
    ("status", "phrase"),
    [
        (503, "answered HTTP 503: it is reachable but not serving downloads right now"),
        (403, "refused the request (HTTP 403): an auth proxy or WAF"),
        (404, "answered HTTP 404: runners are disabled on this instance"),
    ],
)
def test_ps1_a_failed_manifest_names_its_status(tmp_path, dist, status, phrase):
    dist.manifest_status = status
    result = _run_ps1(tmp_path, "-Server", dist.base)
    out = _output(result)
    assert result.returncode != 0
    assert phrase in out
    assert f"last answer: HTTP {status} from {dist.base}" in out


@needs_pwsh
@pytest.mark.parametrize(
    ("setup", "phrase", "answer"),
    [
        (
            {"manifest_body": SIGN_IN_PAGE},
            "answered HTTP 200 without a runner download manifest: an auth proxy or a sign-in page",
            "HTTP 200 without a download manifest",
        ),
        (
            {"manifest_artifacts": {}},
            "has no embedded runner downloads (a source/dev build?)",
            "HTTP 200, no embedded runner downloads",
        ),
        (
            {"manifest_artifacts": {"linux": {"x64": {"url": "http://127.0.0.1:9/x.tar.gz"}}}},
            "does not ship a windows/x64 runner in this release",
            "HTTP 200 without a windows/x64 archive",
        ),
    ],
    ids=["sign-in-page", "no-downloads", "other-platforms-only"],
)
def test_ps1_a_manifest_without_this_archive_says_why(tmp_path, dist, setup, phrase, answer):
    for name, value in setup.items():
        setattr(dist, name, value)
    result = _run_ps1(tmp_path, "-Server", dist.base)
    out = _output(result)
    assert result.returncode != 0
    assert phrase in out
    assert f"last answer: {answer} from {dist.base}" in out


@needs_pwsh
def test_ps1_an_unreachable_platform_is_reported_as_unreachable(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    result = _run_ps1(tmp_path, "-Server", f"http://127.0.0.1:{port}")
    out = _output(result)
    assert result.returncode != 0
    assert "Could not reach http://127.0.0.1:" in out
    assert "last answer: no response" in out


# -- install.ps1: release signature ------------------------------------

VERIFIER_HARNESS = DIST_DIR.parent / "scripts" / "test-install-verifier.ps1"
_ANY_POWERSHELL = PWSH or shutil.which("powershell")


@pytest.mark.skipif(_ANY_POWERSHELL is None, reason="no PowerShell on this host")
def test_the_embedded_verifier_passes_its_checks_on_its_own():
    # Only the verifier section of install.ps1 is loaded: no side effects, so
    # this runs on Windows too (runner-ci runs it under Windows PowerShell 5.1).
    result = subprocess.run(  # noqa: S603
        [
            _ANY_POWERSHELL,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(VERIFIER_HARNESS),
        ],
        env={**os.environ, **_PWSH_QUIET_ENV},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    out = _output(result)
    assert result.returncode == 0, out
    assert "All verifier checks passed" in out
    assert "FAIL" not in out


@needs_pwsh
@pytest.mark.parametrize("sig", _SIGNED_FORMS)
def test_ps1_a_signed_archive_verifies(tmp_path, dist, sig):
    _stage_windows(dist, sig=sig)
    result = _run_ps1(tmp_path, "-Server", dist.base, "-RequireSignature")
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified." in out
    assert "XTM One runner installed." in out
    assert _installed_exe(tmp_path)


@needs_pwsh
def test_ps1_a_mirror_install_verifies_the_signature(tmp_path, dist):
    _stage_windows(dist, sig="valid")
    result = _run_ps1(tmp_path, "-DownloadBase", _mirror(dist), "-RequireSignature")
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified." in out


@needs_pwsh
@pytest.mark.parametrize(
    ("sig", "reason"),
    [
        ("tampered", "does not verify"),
        ("other-key", "signed with another key"),
        ("bad-comment", "its trusted comment does not verify"),
    ],
)
@pytest.mark.parametrize("flag", [(), ("-RequireSignature",)], ids=["default", "switch"])
def test_ps1_a_signature_that_does_not_verify_is_refused_whatever_the_switch(tmp_path, dist, sig, reason, flag):
    _stage_windows(dist, sig=sig)
    result = _run_ps1(tmp_path, "-Server", dist.base, *flag)
    out = _output(result)
    assert result.returncode != 0, out
    assert f"does not verify against the release key ({reason})" in out
    assert not _installed_exe(tmp_path)


@needs_pwsh
@pytest.mark.parametrize(("sig", "phrase"), _NOT_THIS_RELEASE, ids=[s for s, _ in _NOT_THIS_RELEASE])
@pytest.mark.parametrize("flag", [(), ("-RequireSignature",)], ids=["default", "switch"])
def test_ps1_a_signature_the_release_did_not_make_for_this_archive_is_refused(tmp_path, dist, sig, phrase, flag):
    _stage_windows(dist, sig=sig)
    result = _run_ps1(tmp_path, "-Server", dist.base, *flag)
    out = _output(result)
    assert result.returncode != 0, out
    url = f"{dist.base}{DIST_PREFIX}{WIN_ARCHIVE}.minisig"
    assert phrase.format(url=url, archive=WIN_ARCHIVE, other=WIN_ARCHIVE.replace("x64", "arm64")) in out
    assert not _installed_exe(tmp_path)


@needs_pwsh
@pytest.mark.parametrize("flag", [(), ("-RequireSignature",)], ids=["default", "switch"])
def test_ps1_a_signature_that_names_no_file_is_not_tied_to_the_archive(tmp_path, dist, flag):
    _stage_windows(dist, sig="no-file")
    result = _run_ps1(tmp_path, "-Server", dist.base, *flag)
    out = _output(result)
    reason = _names_no_file(f"{dist.base}{DIST_PREFIX}{WIN_ARCHIVE}.minisig", WIN_ARCHIVE)
    assert "Release signature verified" not in out
    if flag:
        assert result.returncode != 0, out
        assert f"Refusing to install an unverified binary: {reason} (-RequireSignature)." in out
        assert not _installed_exe(tmp_path)
    else:
        assert result.returncode == 0, out
        assert f"SIGNATURE NOT VERIFIED: {reason}" in out
        assert _installed_exe(tmp_path)


@needs_pwsh
def test_ps1_a_tauri_cli_signature_verifies_with_the_key_as_tauri_conf_json_carries_it(tmp_path, dist):
    archive, signature = TAURI_SIGNED[WIN_ARCHIVE]
    data = base64.b64decode(archive)
    dist.files[WIN_ARCHIVE] = data
    dist.files[WIN_ARCHIVE + ".sha256"] = _sha_body(WIN_ARCHIVE, data, "valid")
    dist.files[WIN_ARCHIVE + ".minisig"] = base64.b64decode(signature)
    result = _run_ps1(
        tmp_path, "-Server", dist.base, "-RequireSignature", extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": TAURI_SIGNER}
    )
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified." in out
    assert f"{_OVERRIDE_NOTICE} {_TAURI_SIGNER_ID}." in out
    assert _installed_exe(tmp_path)


@needs_pwsh
@pytest.mark.parametrize("value", ["not-a-key", "wrapped-junk"])
def test_ps1_an_unusable_key_override_is_refused_before_downloading(tmp_path, dist, value):
    _stage_windows(dist, sig="valid")
    result = _run_ps1(tmp_path, "-Server", dist.base, extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": _unusable_key(value)})
    out = _output(result)
    assert result.returncode != 0, out
    assert "XTM_RUNNER_MINISIGN_PUBKEY is not a minisign public key" in out
    assert not any(WIN_ARCHIVE in r for r in dist.requests)
    assert not _installed_exe(tmp_path)


@needs_pwsh
@pytest.mark.parametrize("key", list(_SMALL_ORDER_KEYS))
@pytest.mark.parametrize("flag", [(), ("-RequireSignature",)], ids=["default", "switch"])
def test_ps1_a_key_of_small_order_is_refused_whatever_the_switch(tmp_path, dist, key, flag):
    _stage_windows(dist)
    key_line, forged = _small_order_forgery(WIN_ARCHIVE, dist.files[WIN_ARCHIVE], *_SMALL_ORDER_KEYS[key])
    dist.files[WIN_ARCHIVE + ".minisig"] = forged
    result = _run_ps1(tmp_path, "-Server", dist.base, *flag, extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": key_line})
    out = _output(result)
    assert result.returncode != 0, out
    assert "does not verify against the release key (does not verify)" in out
    assert not _installed_exe(tmp_path)


@needs_pwsh
def test_ps1_the_real_release_key_refuses_a_test_signature(tmp_path, dist):
    _stage_windows(dist, sig="valid")
    result = _run_ps1(tmp_path, "-Server", dist.base, extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""})
    out = _output(result)
    assert result.returncode != 0, out
    assert "does not verify against the release key (signed with another key)" in out
    assert not _installed_exe(tmp_path)


@needs_pwsh
def test_ps1_a_key_override_is_announced_with_its_key_id(tmp_path, dist):
    _stage_windows(dist, sig="valid")
    result = _run_ps1(tmp_path, "-Server", dist.base)
    out = _output(result)
    assert result.returncode == 0, out
    assert f"{_OVERRIDE_NOTICE} {_key_id(KEY.pubkey)}." in out


@needs_pwsh
def test_ps1_the_release_key_is_not_announced_as_an_override(tmp_path, dist):
    _stage_windows(dist)
    result = _run_ps1(tmp_path, "-Server", dist.base, extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""})
    out = _output(result)
    assert result.returncode == 0, out
    assert _OVERRIDE_NOTICE not in out


@needs_pwsh
@pytest.mark.parametrize("sig", _NOT_A_SIGNATURE)
@pytest.mark.parametrize("flag", [(), ("-RequireSignature",)], ids=["default", "switch"])
def test_ps1_a_minisig_that_is_not_a_signature_counts_as_a_missing_one(tmp_path, dist, sig, flag):
    _stage_windows(dist, sig=sig)
    result = _run_ps1(tmp_path, "-Server", dist.base, *flag)
    out = _output(result)
    reason = f"the .minisig at {dist.base}{DIST_PREFIX}{WIN_ARCHIVE}.minisig is not a minisign signature"
    assert "Release signature verified" not in out
    if flag:
        assert result.returncode != 0, out
        assert f"Refusing to install an unverified binary: {reason} (-RequireSignature)." in out
        assert not _installed_exe(tmp_path)
    else:
        assert result.returncode == 0, out
        assert f"SIGNATURE NOT VERIFIED: {reason}" in out
        assert _installed_exe(tmp_path)


@needs_pwsh
@pytest.mark.parametrize(
    ("sig", "published", "phrase"),
    [
        (None, True, "no .minisig could be fetched from {url} (HTTP 404)"),
        ("empty", True, "the .minisig at {url} is empty"),
        ("valid", False, "no .minisig is published next to the archive"),
    ],
    ids=["missing", "empty", "unpublished"],
)
def test_ps1_an_unchecked_signature_is_a_loud_warning(tmp_path, dist, sig, published, phrase):
    _stage_windows(dist, sig=sig)
    dist.manifest_signature = published
    result = _run_ps1(tmp_path, "-Server", dist.base)
    out = _output(result)
    assert result.returncode == 0, out
    reason = phrase.format(url=f"{dist.base}{DIST_PREFIX}{WIN_ARCHIVE}.minisig")
    assert f"SIGNATURE NOT VERIFIED: {reason}" in out
    assert "WITHOUT signature verification" in out
    assert _installed_exe(tmp_path)


@needs_pwsh
@pytest.mark.parametrize(
    ("flag", "env"),
    [
        (("-RequireSignature",), None),
        ((), {"XTM_RUNNER_REQUIRE_SIGNATURE": "1"}),
    ],
    ids=["switch", "env-1"],
)
def test_ps1_require_signature_refuses_an_unsigned_archive(tmp_path, dist, flag, env):
    _stage_windows(dist)
    result = _run_ps1(tmp_path, "-Server", dist.base, *flag, extra_env=env)
    out = _output(result)
    assert result.returncode != 0, out
    assert "Refusing to install an unverified binary: no .minisig could be fetched" in out
    assert not _installed_exe(tmp_path)


# -- On top of the verifier: the installed runner, a bounded read, the image build --------

RUNNER_PAYLOAD = b"#!/bin/sh\necho fake-runner\n"

# An installed runner, as install.sh finds it on an upgrade: `help
# verify-signature` answers 0, and `verify-signature --pubkey K --signature S
# FILE` exits 0 verified, 1 does not verify, 3 could not check
# (apps/runner/src/main.rs). "old" is a runner from before the command
# (clap's usage error, 2); "yes" accepts anything. It parses the signature
# like the runner's minisign-verify 0.2.5: the lines as given, and base64
# with no whitespace, exact padding and no bit set that the last character
# does not use. The interpreter is named outright: the restricted PATH of
# these runs holds no python.
_RUNNER_STAND_IN = """#!{python}
import base64, hashlib, sys
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MODE = {mode!r}
args = sys.argv[1:]
if MODE == "old" or not args:
    sys.exit(2)
if args[:2] == ["help", "verify-signature"]:
    sys.exit(0)
if args[0] != "verify-signature":
    sys.exit(2)
if MODE == "yes":
    sys.exit(0)
options = dict(zip(args[1:-1:2], args[2:-1:2]))


def canonical(text):
    raw = base64.b64decode(text, validate=True)
    if base64.b64encode(raw).decode() != text:
        raise ValueError(text)
    return raw


try:
    pk = canonical(options["--pubkey"])
    with open(options["--signature"], encoding="utf-8") as handle:
        lines = [line.removesuffix("\\r") for line in handle.read().strip().split("\\n")]
    sig, gsig = canonical(lines[1]), canonical(lines[3])
    if len(sig) != 74 or len(gsig) != 64 or not lines[2].startswith("trusted comment: "):
        raise ValueError(lines)
    comment = lines[2][len("trusted comment: "):]
    with open(args[-1], "rb") as handle:
        data = handle.read()
except Exception:
    sys.exit(3)
if sig[:2] != b"ED" or sig[2:10] != pk[2:10]:
    sys.exit(1)
key = Ed25519PublicKey.from_public_bytes(pk[10:])
try:
    key.verify(sig[10:], hashlib.blake2b(data, digest_size=64).digest())
    key.verify(gsig, sig[10:] + comment.encode())
except InvalidSignature:
    sys.exit(1)
sys.exit(0)
"""


def _install_runner_stand_in(tmp_path: Path, mode: str) -> Path:
    runner = _root(tmp_path) / "bin" / "xtm-one-runner"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.write_text(_RUNNER_STAND_IN.format(python=sys.executable, mode=mode), encoding="utf-8")
    runner.chmod(0o755)
    # Reading the .minisig takes cat, tail and cp, which the restricted PATH of
    # a run without minisign / OpenSSL (``verifier=False``) does not link: every
    # host has them.
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    for tool in ("cat", "tail", "cp"):
        real = shutil.which(tool)
        if real is None:
            pytest.skip(f"{tool} is not available")
        (bin_dir / tool).symlink_to(real)
    return runner


def _upgraded(tmp_path: Path) -> bool:
    # The archive's binary replaced the runner that was installed.
    return (_root(tmp_path) / "bin" / "xtm-one-runner").read_bytes() == RUNNER_PAYLOAD


@needs_bash
@pytest.mark.parametrize("sig", _SIGNED_FORMS)
def test_an_installed_runner_verifies_where_neither_minisign_nor_openssl_can(tmp_path, dist, sig):
    # A stock Mac: LibreSSL and no minisign. On an upgrade the runner already
    # installed checks the signature, the self-updater's own way.
    _install_runner_stand_in(tmp_path, "real")
    _stage(dist, sig=sig)
    result = _run(tmp_path, "--download-base", _mirror(dist), "--require-signature", verifier=False)
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified (runner)." in out
    assert _upgraded(tmp_path)


# base64 as the uutils coreutils of Ubuntu 25.10 and later decode it: a last
# character with unused bits set is "invalid input", where GNU base64 and
# OpenSSL ignore those bits.
_STRICT_BASE64 = """#!{python}
import base64, binascii, sys

data = sys.stdin.buffer.read()
if not any(arg in ("-d", "-D", "--decode") for arg in sys.argv[1:]):
    sys.stdout.buffer.write(base64.b64encode(data) + b"\\n")
    sys.exit(0)
text = b"".join(data.split())
try:
    decoded = base64.b64decode(text, validate=True)
except binascii.Error:
    decoded = None
if decoded is None or base64.b64encode(decoded) != text:
    sys.stderr.write("base64: error: invalid input\\n")
    sys.exit(1)
sys.stdout.buffer.write(decoded)
"""


@needs_bash
@pytest.mark.parametrize("sig", _SIGNED_FORMS)
def test_a_base64_that_refuses_unused_bits_reads_every_signed_form(tmp_path, dist, sig):
    # The only decoder of a host without minisign or OpenSSL is base64, and on
    # Ubuntu 25.10+ it refuses what GNU base64 reads.
    _install_runner_stand_in(tmp_path, "real")
    strict = tmp_path / "fakebin" / "base64"
    strict.write_text(_STRICT_BASE64.format(python=sys.executable), encoding="utf-8")
    strict.chmod(0o755)
    _stage(dist, sig=sig)
    result = _run(tmp_path, "--download-base", _mirror(dist), "--require-signature", verifier=False)
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified (runner)." in out
    assert _upgraded(tmp_path)


@needs_bash
@pytest.mark.parametrize(
    ("sig", "phrase"),
    [
        ("tampered", "does not verify against the release key (does not verify)"),
        ("bad-comment", "does not verify against the release key (does not verify)"),
        ("other-key", "does not verify against the release key (signed with another key)"),
        ("other-archive", "is not for xtm-one-runner-linux-x64.tar.gz"),
    ],
)
def test_an_installed_runner_refuses_what_the_verifier_refuses(tmp_path, dist, sig, phrase):
    # Main's checks still run around it: the key id, and the file the trusted
    # comment names.
    _install_runner_stand_in(tmp_path, "real")
    _stage(dist, sig=sig)
    result = _run(tmp_path, "--download-base", _mirror(dist), verifier=False)
    out = _output(result)
    assert result.returncode != 0, out
    assert phrase in out
    assert not _upgraded(tmp_path)


@needs_bash
def test_an_installed_runner_is_not_asked_about_an_r_of_small_order(tmp_path, dist):
    # minisign-verify, like OpenSSL, accepts a global signature whose R has
    # small order: the script refuses it before any verifier runs.
    key_line, archive, signature = MIXED_ORDER_SIGNED
    _install_runner_stand_in(tmp_path, "real")
    data = base64.b64decode(archive)
    dist.files[ARCHIVE] = data
    dist.files[ARCHIVE + ".sha256"] = _sha_body(ARCHIVE, data, "valid")
    dist.files[ARCHIVE + ".minisig"] = base64.b64decode(signature)
    result = _run(
        tmp_path, "--download-base", _mirror(dist), verifier=False, extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": key_line}
    )
    out = _output(result)
    assert result.returncode != 0, out
    assert "does not verify against the release key (its trusted comment does not verify)" in out
    assert not _upgraded(tmp_path)


@needs_bash
@pytest.mark.parametrize("mode", ["old", "yes"])
def test_an_installed_runner_that_fails_the_self_test_is_not_used(tmp_path, dist, mode):
    # A runner without the command, or one that says yes to anything, reads as
    # "cannot check": the tampered archive is not reported as verified.
    _install_runner_stand_in(tmp_path, mode)
    _stage(dist, sig="tampered")
    result = _run(tmp_path, "--download-base", _mirror(dist), verifier=False)
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified" not in out
    assert "SIGNATURE NOT VERIFIED: neither minisign nor OpenSSL 3" in out


@needs_bash
def test_the_installed_runner_is_the_last_resort(tmp_path, dist):
    _install_runner_stand_in(tmp_path, "real")
    _stage(dist, sig="valid")
    result = _run(tmp_path, "--download-base", _mirror(dist), "--require-signature")
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified" in out
    assert "Release signature verified (runner)." not in out


# minisign as install.sh runs it (`minisign -V -q -m FILE -x SIG -P KEY`),
# parsing like the C tool: an untrusted comment that starts with "untrusted
# comment: ", and base64 lines read as they are - whole groups of the alphabet,
# the unused bits of the last character ignored. The CI host has no minisign.
_MINISIGN_STAND_IN = """#!{python}
import base64, hashlib, re, sys
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

options = {{}}
args = iter(sys.argv[1:])
for arg in args:
    if arg in ("-m", "-x", "-P"):
        options[arg] = next(args)


def b64(text):
    if not re.fullmatch(r"(?:[A-Za-z0-9+/]{{4}})*(?:[A-Za-z0-9+/]{{2}}==|[A-Za-z0-9+/]{{3}}=)?", text):
        raise ValueError(text)
    return base64.b64decode(text)


try:
    pk = b64(options["-P"])
    with open(options["-x"], encoding="utf-8") as handle:
        lines = [line.removesuffix("\\r") for line in handle.read().split("\\n")]
    if not lines[0].startswith("untrusted comment: ") or not lines[2].startswith("trusted comment: "):
        raise ValueError(lines)
    sig, gsig = b64(lines[1]), b64(lines[3])
    comment = lines[2][len("trusted comment: "):]
    with open(options["-m"], "rb") as handle:
        data = handle.read()
except Exception:
    sys.exit(1)
if sig[:2] != b"ED" or sig[2:10] != pk[2:10]:
    sys.exit(1)
key = Ed25519PublicKey.from_public_bytes(pk[10:])
try:
    key.verify(sig[10:], hashlib.blake2b(data, digest_size=64).digest())
    key.verify(gsig, sig[10:] + comment.encode())
except InvalidSignature:
    sys.exit(1)
sys.exit(0)
"""


@needs_bash
@pytest.mark.parametrize("sig", _SIGNED_FORMS)
def test_minisign_verifies_what_the_other_verifiers_verify(tmp_path, dist, sig):
    # minisign is handed the lines the script checked, not the ones served.
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    minisign = bin_dir / "minisign"
    minisign.write_text(_MINISIGN_STAND_IN.format(python=sys.executable), encoding="utf-8")
    minisign.chmod(0o755)
    _stage(dist, sig=sig)
    result = _run(tmp_path, "--download-base", _mirror(dist), "--require-signature")
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified (minisign)." in out
    assert _installed(tmp_path)


@needs_bash
@pytest.mark.parametrize("sized", [True, False], ids=["sized", "unsized"])
@pytest.mark.parametrize("flag", [(), ("--require-signature",)], ids=["default", "require"])
def test_an_oversized_minisig_is_not_read_and_counts_as_a_missing_one(tmp_path, dist, flag, sized):
    # Never read more than a signature can be: curl stops at 64 KB, and the
    # script checks the size where an older curl read a body of no announced
    # size to the end.
    _stage(dist)
    dist.files[ARCHIVE + ".minisig"] = b"A" * 100_000
    if not sized:
        dist.unsized.add(ARCHIVE + ".minisig")
    result = _run(tmp_path, "--download-base", _mirror(dist), *flag)
    out = _output(result)
    reason = f"the .minisig at {_mirror(dist)}/{ARCHIVE}.minisig is larger than a minisign signature can be"
    if flag:
        assert result.returncode != 0, out
        assert f"Refusing to install an unverified binary: {reason} (--require-signature)." in out
        assert not _installed(tmp_path)
    else:
        assert result.returncode == 0, out
        assert f"SIGNATURE NOT VERIFIED: {reason}" in out
        assert _installed(tmp_path)


@needs_pwsh
@pytest.mark.parametrize("sized", [True, False], ids=["sized", "unsized"])
@pytest.mark.parametrize("flag", [(), ("-RequireSignature",)], ids=["default", "switch"])
def test_ps1_an_oversized_minisig_is_not_read_and_counts_as_a_missing_one(tmp_path, dist, flag, sized):
    # Invoke-WebRequest would buffer any size: the script stops at 64 KB.
    _stage_windows(dist)
    dist.files[WIN_ARCHIVE + ".minisig"] = b"A" * 100_000
    if not sized:
        dist.unsized.add(WIN_ARCHIVE + ".minisig")
    result = _run_ps1(tmp_path, "-Server", dist.base, *flag)
    out = _output(result)
    reason = f"the .minisig at {dist.base}{DIST_PREFIX}{WIN_ARCHIVE}.minisig is larger than a minisign signature can be"
    if flag:
        assert result.returncode != 0, out
        assert f"Refusing to install an unverified binary: {reason} (-RequireSignature)." in out
        assert not _installed_exe(tmp_path)
    else:
        assert result.returncode == 0, out
        assert f"SIGNATURE NOT VERIFIED: {reason}" in out
        assert _installed_exe(tmp_path)


# The image build writes the release keys and the signature floor of its own
# release-signing.json into the scripts it serves
# (apps/runner/dist/embed_release_key.py, run by the Dockerfile), so an
# instance built and signed with other keys - a fork - hands out scripts that
# trust them without XTM_RUNNER_MINISIGN_PUBKEY.

REPO_ROOT = DIST_DIR.parents[2]
EMBED_SCRIPT = DIST_DIR / "embed_release_key.py"


def _load_embed_module():
    spec = importlib.util.spec_from_file_location("embed_release_key", EMBED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


EMBED = _load_embed_module()


@pytest.mark.parametrize("script", [INSTALL_SH, INSTALL_PS1], ids=["install.sh", "install.ps1"])
def test_the_image_build_rewrites_the_key_and_floor_lines_the_script_reads(script):
    # Exactly one line of each matches, and it holds what the release trusts.
    with script.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    keys, floor = EMBED.release_trust(RELEASE_SIGNING)
    assert [match[1] for match in EMBED.KEY_LINES[script.suffix].findall(text)] == [" ".join(keys)]
    assert [match[1] for match in EMBED.FLOOR_LINES[script.suffix].findall(text)] == [str(floor)]


def test_the_image_build_writes_the_release_keys_into_the_served_scripts():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    # The RUN instruction with its continuation lines.
    run = re.search(
        r"^RUN python \S+embed_release_key\.py \S+release-signing\.json(?:[^\n]*\\\n)*[^\n]*", dockerfile, re.M
    )
    assert run, "the Dockerfile no longer runs embed_release_key.py over release-signing.json"
    assert "/app/runner_dist/xtm-one-runner-installer.sh" in run.group(0)
    assert "/app/runner_dist/xtm-one-runner-installer.ps1" in run.group(0)
    assert "COPY apps/runner/release-signing.json apps/runner/dist/embed_release_key.py" in dockerfile
    # After the scripts are copied into the dist dir, never before.
    assert dockerfile.index("xtm-one-runner-installer.ps1\n") < run.start()


def _run_embed(
    tmp_path: Path, conf: dict | str
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path], dict[str, str]]:
    """Run embed_release_key.py over fresh copies of both scripts, as the image build does."""
    work = tmp_path / "embed"
    work.mkdir()
    (work / "release-signing.json").write_text(conf if isinstance(conf, str) else json.dumps(conf), encoding="utf-8")
    served = {}
    before = {}
    for source, name in ((INSTALL_SH, "xtm-one-runner-installer.sh"), (INSTALL_PS1, "xtm-one-runner-installer.ps1")):
        served[source.suffix] = work / name
        shutil.copy2(source, work / name)
        with (work / name).open(encoding="utf-8", newline="") as handle:
            before[source.suffix] = handle.read()
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(EMBED_SCRIPT),
            str(work / "release-signing.json"),
            *(str(path) for path in served.values()),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, served, before


def _tauri_pubkey(key_line: str) -> str:
    """How tauri.conf.json holds a key: the base64 of a whole minisign .pub file."""
    return base64.b64encode(f"untrusted comment: minisign public key: 0123\n{key_line}\n".encode()).decode()


def test_the_image_build_embeds_its_own_keys_and_floor_and_touches_nothing_else(tmp_path):
    result, served, before = _run_embed(
        tmp_path, {"keys": [OTHER_KEY.pubkey, KEY.pubkey], "timestamp_floor": 1790000001}
    )
    assert result.returncode == 0, result.stderr
    assert KEY.pubkey not in result.stdout + result.stderr
    for suffix, path in served.items():
        with path.open(encoding="utf-8", newline="") as handle:
            after = handle.read()
        changed = [
            (old, new)
            for old, new in zip(before[suffix].splitlines(True), after.splitlines(True), strict=True)
            if old != new
        ]
        assert len(changed) == 2, suffix
        assert f"{OTHER_KEY.pubkey} {KEY.pubkey}" in changed[0][1]
        assert "1790000001" in changed[1][1]
        # Line endings survive: install.ps1 is checked out with CRLF (``eol=crlf``).
        for old, new in changed:
            assert old.endswith("\r\n") == new.endswith("\r\n")


def _trust(*keys: _SigningKey, floor: int = 0) -> dict:
    return {"keys": [key.pubkey for key in keys], "timestamp_floor": floor}


@needs_bash
def test_a_script_the_image_build_rewrote_trusts_its_key(tmp_path, dist):
    # With no XTM_RUNNER_MINISIGN_PUBKEY, the served install.sh trusts the key
    # the build wrote into it.
    result, served, _ = _run_embed(tmp_path, _trust(KEY))
    assert result.returncode == 0, result.stderr
    _stage(dist, sig="valid")
    result = _run(
        tmp_path,
        "--download-base",
        _mirror(dist),
        "--require-signature",
        extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""},
        script=served[".sh"],
    )
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified" in out


@needs_pwsh
def test_ps1_a_script_the_image_build_rewrote_trusts_its_key(tmp_path, dist):
    result, served, _ = _run_embed(tmp_path, _trust(KEY))
    assert result.returncode == 0, result.stderr
    _stage_windows(dist, sig="valid")
    result = _run_ps1(
        tmp_path,
        "-Server",
        dist.base,
        "-RequireSignature",
        extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""},
        script=served[".ps1"],
    )
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified" in out


@pytest.mark.parametrize(
    ("conf", "reason"),
    [
        ("not json", "cannot read"),
        ({}, "keys must list 1 to 4 minisign key lines"),
        ({"keys": []}, "keys must list 1 to 4 minisign key lines"),
        ({"keys": [KEY.pubkey] * 5}, "keys must list 1 to 4 minisign key lines"),
        ({"keys": [KEY.pubkey, "not-a-key"]}, "'not-a-key' is not a minisign key line"),
        ({"keys": [KEY.pubkey, KEY.pubkey]}, "two keys share the key id"),
        ({"keys": [KEY.pubkey], "timestamp_floor": -1}, "timestamp_floor must be a Unix time"),
        ({"keys": [KEY.pubkey], "timestamp_floor": "1790000000"}, "timestamp_floor must be a Unix time"),
        ({"keys": [KEY.pubkey], "timestamp_floor": True}, "timestamp_floor must be a Unix time"),
    ],
    ids=["not-json", "no-keys", "empty", "too-many", "not-a-key", "same-id", "negative", "string", "bool"],
)
def test_the_image_build_keeps_the_scripts_as_they_are_without_a_usable_trust_file(tmp_path, conf, reason):
    result, served, before = _run_embed(tmp_path, conf)
    assert result.returncode == 0, result.stderr
    assert reason in result.stderr
    assert "the install scripts keep the keys they carry" in result.stderr
    for suffix, path in served.items():
        with path.open(encoding="utf-8", newline="") as handle:
            assert handle.read() == before[suffix]


@pytest.mark.parametrize(
    ("script", "what"),
    [
        ("#!/usr/bin/env bash\necho no key here\n", "release key"),
        (f'#!/usr/bin/env bash\nRELEASE_PUBKEYS="{KEY.pubkey}"\n', "signature floor"),
    ],
    ids=["no-key-line", "no-floor-line"],
)
@pytest.mark.parametrize("conf", [_trust(KEY), {}], ids=["with-keys", "without-keys"])
def test_the_image_build_refuses_a_script_without_its_key_or_floor_line(tmp_path, script, what, conf):
    # Checked before the trust file is read: a script that lost one of its lines
    # is a repository bug, whatever the trust file holds.
    work = tmp_path / "embed"
    work.mkdir()
    (work / "release-signing.json").write_text(json.dumps(conf), encoding="utf-8")
    (work / "xtm-one-runner-installer.sh").write_text(script, encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(EMBED_SCRIPT),
            str(work / "release-signing.json"),
            str(work / "xtm-one-runner-installer.sh"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert f"expected one {what} line, found 0" in result.stderr


# -- several release keys, the version signed, the signature floor -----------------

# What `sign-release-archive.mjs` (the release) writes: tauri signer's trusted
# comment with the runner version added.
RUNNER_VERSION = "1.260928.57"


def _versioned(name: str, version: str = RUNNER_VERSION, timestamp: int = 1790000100) -> str:
    return f"timestamp:{timestamp}\tfile:{name}\tversion:{version}"


def _served_script(tmp_path: Path, suffix: str, conf: dict) -> Path:
    """The script an instance whose release-signing.json is ``conf`` serves."""
    result, served, _ = _run_embed(tmp_path, conf)
    assert result.returncode == 0, result.stderr
    return served[suffix]


@needs_bash
@pytest.mark.parametrize("signer", [KEY, OTHER_KEY], ids=["first-key", "second-key"])
def test_the_key_id_a_signature_names_picks_one_of_the_release_keys(tmp_path, dist, signer):
    # A rotation is staged: scripts trust the next key before the release signs
    # with it, and the old one until it is retired.
    script = _served_script(tmp_path, ".sh", _trust(KEY, OTHER_KEY))
    _stage(dist)
    dist.files[ARCHIVE + ".minisig"] = signer.sign(dist.files[ARCHIVE], name=ARCHIVE).encode()
    result = _run(
        tmp_path,
        "--download-base",
        _mirror(dist),
        "--require-signature",
        extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""},
        script=script,
    )
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified" in out
    assert f"Signed with key ID {_key_id(signer.pubkey)}." in out
    assert _OVERRIDE_NOTICE not in out


@needs_bash
def test_a_key_that_is_not_a_release_key_is_refused(tmp_path, dist):
    script = _served_script(tmp_path, ".sh", _trust(KEY, OTHER_KEY))
    _stage(dist)
    dist.files[ARCHIVE + ".minisig"] = THIRD_KEY.sign(dist.files[ARCHIVE], name=ARCHIVE).encode()
    result = _run(
        tmp_path, "--download-base", _mirror(dist), extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""}, script=script
    )
    out = _output(result)
    assert result.returncode != 0, out
    assert "does not verify against the release key (signed with another key)" in out
    assert not _installed(tmp_path)


@needs_pwsh
@pytest.mark.parametrize(
    ("signer", "expect_installed"),
    [(KEY, True), (OTHER_KEY, True), (THIRD_KEY, False)],
    ids=["first-key", "second-key", "not-a-release-key"],
)
def test_ps1_the_key_id_a_signature_names_picks_one_of_the_release_keys(tmp_path, dist, signer, expect_installed):
    script = _served_script(tmp_path, ".ps1", _trust(KEY, OTHER_KEY))
    _stage_windows(dist)
    dist.files[WIN_ARCHIVE + ".minisig"] = signer.sign(dist.files[WIN_ARCHIVE], name=WIN_ARCHIVE).encode()
    result = _run_ps1(tmp_path, "-Server", dist.base, extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""}, script=script)
    out = _output(result)
    assert (result.returncode == 0) is expect_installed, out
    assert _installed_exe(tmp_path) is expect_installed
    if expect_installed:
        assert f"Signed with key ID {_key_id(signer.pubkey)}." in out
    else:
        assert "does not verify against the release key (signed with another key)" in out


def _stage_signed(dist: _Dist, name: str, trusted: str, *, windows: bool = False) -> None:
    if windows:
        _stage_windows(dist)
    else:
        _stage(dist)
    dist.files[name + ".minisig"] = KEY.sign(dist.files[name], name=name, trusted=trusted).encode()


@needs_bash
def test_the_version_the_platform_ships_installs(tmp_path, dist):
    _stage_signed(dist, ARCHIVE, _versioned(ARCHIVE))
    dist.manifest_runner_version = RUNNER_VERSION
    result = _run(tmp_path, "--server", dist.base, "--require-signature")
    out = _output(result)
    assert result.returncode == 0, out
    assert f"GET {MANIFEST_PATH}" in dist.requests
    assert f"Signed with key ID {_key_id(KEY.pubkey)} for runner {RUNNER_VERSION}." in out
    assert _installed(tmp_path)


@needs_bash
@pytest.mark.parametrize("flag", [(), ("--require-signature",)], ids=["default", "require"])
def test_another_signed_version_served_by_the_platform_is_refused(tmp_path, dist, flag):
    # Every archive name is the same in every release: an older build and its
    # genuine signature pass every other check.
    _stage_signed(dist, ARCHIVE, _versioned(ARCHIVE, "1.260901.3"))
    dist.manifest_runner_version = RUNNER_VERSION
    result = _run(tmp_path, "--server", dist.base, *flag)
    out = _output(result)
    assert result.returncode != 0, out
    assert (
        f"the signature at {dist.base}{DIST_PREFIX}{ARCHIVE}.minisig is for runner 1.260901.3, not {RUNNER_VERSION}, "
        f"the version {dist.base} ships - another signed build of {ARCHIVE} was served in its place"
    ) in out
    assert not _installed(tmp_path)


@needs_bash
@pytest.mark.parametrize(
    ("setup", "sig_version"),
    [
        # Signed before versions were: the release keeps installing.
        ({"manifest_runner_version": RUNNER_VERSION}, None),
        # A platform that does not say which version it ships.
        ({}, "1.260901.3"),
        # The manifest cannot be read.
        ({"manifest_runner_version": RUNNER_VERSION, "manifest_status": 503}, "1.260901.3"),
    ],
    ids=["unversioned-signature", "no-runner-version", "manifest-unavailable"],
)
def test_a_version_that_cannot_be_compared_is_not_refused(tmp_path, dist, setup, sig_version):
    trusted = _versioned(ARCHIVE, sig_version) if sig_version else f"timestamp:1790000000\tfile:{ARCHIVE}"
    _stage_signed(dist, ARCHIVE, trusted)
    for name, value in setup.items():
        setattr(dist, name, value)
    result = _run(tmp_path, "--server", dist.base, "--require-signature")
    out = _output(result)
    assert result.returncode == 0, out
    assert "Release signature verified" in out
    if sig_version is None:
        assert "The signature names no runner version (signed before release signatures carried one)" in out
    else:
        assert f"for runner {sig_version}." in out
    assert _installed(tmp_path)


@needs_bash
def test_a_mirror_install_names_the_signed_version(tmp_path, dist):
    # A mirror has no manifest: the version is shown, nothing to compare it with.
    _stage_signed(dist, ARCHIVE, _versioned(ARCHIVE, "1.260901.3"))
    result = _run(tmp_path, "--download-base", _mirror(dist), "--require-signature")
    out = _output(result)
    assert result.returncode == 0, out
    assert "for runner 1.260901.3." in out
    assert f"GET {MANIFEST_PATH}" not in dist.requests


@needs_pwsh
@pytest.mark.parametrize(
    ("sig_version", "shipped", "expect_installed"),
    [
        (RUNNER_VERSION, RUNNER_VERSION, True),
        ("1.260901.3", RUNNER_VERSION, False),
        (None, RUNNER_VERSION, True),
        ("1.260901.3", None, True),
    ],
    ids=["shipped", "older", "unversioned-signature", "no-runner-version"],
)
def test_ps1_the_signed_version_is_the_one_the_platform_ships(tmp_path, dist, sig_version, shipped, expect_installed):
    trusted = _versioned(WIN_ARCHIVE, sig_version) if sig_version else f"timestamp:1790000000\tfile:{WIN_ARCHIVE}"
    _stage_signed(dist, WIN_ARCHIVE, trusted, windows=True)
    dist.manifest_runner_version = shipped
    result = _run_ps1(tmp_path, "-Server", dist.base, "-RequireSignature")
    out = _output(result)
    assert (result.returncode == 0) is expect_installed, out
    assert _installed_exe(tmp_path) is expect_installed
    if not expect_installed:
        assert (
            f"the signature at {dist.base}{DIST_PREFIX}{WIN_ARCHIVE}.minisig is for runner {sig_version}, "
            f"not {shipped}, the version the platform ships - another signed build of {WIN_ARCHIVE} was served"
        ) in out
    elif sig_version:
        assert f"Signed with key ID {_key_id(KEY.pubkey)} for runner {sig_version}." in out
    else:
        assert "The signature names no runner version" in out


@needs_bash
@pytest.mark.parametrize(
    ("timestamp", "expect_installed"),
    [(1790000000, False), (1790000001, True), (1890000000, True)],
    ids=["older", "at-the-floor", "newer"],
)
def test_a_signature_older_than_the_floor_is_refused(tmp_path, dist, timestamp, expect_installed):
    script = _served_script(tmp_path, ".sh", _trust(KEY, floor=1790000001))
    _stage_signed(dist, ARCHIVE, f"timestamp:{timestamp}\tfile:{ARCHIVE}")
    result = _run(
        tmp_path, "--download-base", _mirror(dist), extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""}, script=script
    )
    out = _output(result)
    assert (result.returncode == 0) is expect_installed, out
    assert _installed(tmp_path) is expect_installed
    if not expect_installed:
        assert (
            f"the signature at {_mirror(dist)}/{ARCHIVE}.minisig was made at {timestamp}, before 1790000001, the "
            "oldest release signature this script accepts - an older signed build was served in its place"
        ) in out


@needs_bash
@pytest.mark.parametrize(
    ("comment", "phrase"),
    [
        ("timestamp:yesterday", "was made at yesterday, before 1790000001"),
        ("", "names no signing time, so nothing says it is newer than 1790000001"),
    ],
    ids=["not-a-time", "no-timestamp"],
)
def test_a_timestamp_the_floor_cannot_read(tmp_path, dist, comment, phrase):
    # Every release signature names its time: one that is not a time, or none,
    # cannot be proven newer than the floor.
    script = _served_script(tmp_path, ".sh", _trust(KEY, floor=1790000001))
    _stage_signed(dist, ARCHIVE, f"{comment}\tfile:{ARCHIVE}" if comment else f"file:{ARCHIVE}")
    result = _run(
        tmp_path, "--download-base", _mirror(dist), extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""}, script=script
    )
    out = _output(result)
    assert result.returncode != 0, out
    assert phrase in out
    assert not _installed(tmp_path)


@needs_bash
def test_a_key_override_brings_no_floor(tmp_path, dist):
    # The floor is the release keys' own: a fork's key is not held to it.
    script = _served_script(tmp_path, ".sh", _trust(OTHER_KEY, floor=1790000001))
    _stage_signed(dist, ARCHIVE, f"timestamp:1790000000\tfile:{ARCHIVE}")
    result = _run(tmp_path, "--download-base", _mirror(dist), "--require-signature", script=script)
    out = _output(result)
    assert result.returncode == 0, out
    assert f"{_OVERRIDE_NOTICE} {_key_id(KEY.pubkey)}." in out


@needs_pwsh
@pytest.mark.parametrize(
    ("timestamp", "override", "expect_installed"),
    [(1790000000, False, False), (1790000001, False, True), (1790000000, True, True)],
    ids=["older", "at-the-floor", "override"],
)
def test_ps1_a_signature_older_than_the_floor_is_refused(tmp_path, dist, timestamp, override, expect_installed):
    script = _served_script(tmp_path, ".ps1", _trust(OTHER_KEY if override else KEY, floor=1790000001))
    _stage_signed(dist, WIN_ARCHIVE, f"timestamp:{timestamp}\tfile:{WIN_ARCHIVE}", windows=True)
    env = {} if override else {"XTM_RUNNER_MINISIGN_PUBKEY": ""}
    result = _run_ps1(tmp_path, "-Server", dist.base, extra_env=env, script=script)
    out = _output(result)
    assert (result.returncode == 0) is expect_installed, out
    assert _installed_exe(tmp_path) is expect_installed
    if not expect_installed:
        assert (f"was made at {timestamp}, before 1790000001, the oldest release signature this script accepts") in out


@needs_pwsh
def test_ps1_a_signature_that_names_no_time_is_refused_under_a_floor(tmp_path, dist):
    script = _served_script(tmp_path, ".ps1", _trust(KEY, floor=1790000001))
    _stage_signed(dist, WIN_ARCHIVE, f"file:{WIN_ARCHIVE}", windows=True)
    result = _run_ps1(tmp_path, "-Server", dist.base, extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""}, script=script)
    out = _output(result)
    assert result.returncode != 0, out
    assert not _installed_exe(tmp_path)
    assert "names no signing time, so nothing says it is newer than 1790000001" in out


# -- The release signer (apps/runner/scripts/sign-release-archive.mjs) -------------------

SIGNER = DIST_DIR.parent / "scripts" / "sign-release-archive.mjs"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="the release signer runs on Node")


def _tauri_secret_key(label: str, password: str) -> tuple[str, str]:
    """A throwaway key as ``tauri signer generate`` writes it (rust-minisign's format).

    The base64 of the whole secret key file, and the key line of its public
    half. "Ed", kdf "Sc", checksum "B2", salt, opslimit and memlimit (the
    defaults, scrypt N = 2**15, r = 8, p = 1), then key id + seed + public key
    + BLAKE2b-256 checksum, sealed with the scrypt stream of the password - an
    empty one too, as rust-minisign seals a key generated without a password.
    """
    seed = hashlib.sha256(f"seed of {label}".encode()).digest()
    key_id = hashlib.sha256(f"key id of {label}".encode()).digest()[:8]
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    plain = key_id + seed + public + hashlib.blake2b(b"Ed" + key_id + seed + public, digest_size=32).digest()
    salt = hashlib.sha256(f"salt of {label}".encode()).digest()
    stream = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=104)
    plain = bytes(a ^ b for a, b in zip(plain, stream, strict=True))
    raw = b"EdScB2" + salt + (1_048_576).to_bytes(8, "little") + (33_554_432).to_bytes(8, "little") + plain
    text = f"untrusted comment: rsign encrypted secret key\n{base64.b64encode(raw).decode()}\n"
    return base64.b64encode(text.encode()).decode(), base64.b64encode(b"Ed" + key_id + public).decode()


def _sign_release(archive: Path, version: str, secret: str, password: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [NODE, str(SIGNER), str(archive), version],
        env={**os.environ, "TAURI_SIGNING_PRIVATE_KEY": secret, "TAURI_SIGNING_PRIVATE_KEY_PASSWORD": password},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@needs_node
@pytest.mark.parametrize("password", ["correct horse battery", ""], ids=["password", "no-password"])
def test_the_release_signer_signs_the_archive_and_its_version(tmp_path, password):
    secret, key_line = _tauri_secret_key("release signer test key", password)
    archive = tmp_path / ARCHIVE
    archive.write_bytes(_archive_bytes())
    result = _sign_release(archive, RUNNER_VERSION, secret, password)
    assert result.returncode == 0, result.stderr
    assert f"signed with key ID {_key_id(key_line)} for runner {RUNNER_VERSION}" in result.stdout
    assert secret not in result.stdout + result.stderr
    lines = (tmp_path / f"{ARCHIVE}.minisig").read_text(encoding="utf-8").split("\n")
    assert lines[0] == "untrusted comment: signature from tauri secret key"
    assert lines[4:] == [""]
    signature = base64.b64decode(lines[1], validate=True)
    comment = lines[2].removeprefix("trusted comment: ")
    fields = dict(field.split(":", 1) for field in comment.split("\t"))
    assert list(fields) == ["timestamp", "file", "version"]
    assert fields["file"] == ARCHIVE
    assert fields["version"] == RUNNER_VERSION
    assert fields["timestamp"].isdigit()
    pk = base64.b64decode(key_line)
    assert signature[:10] == b"ED" + pk[2:10]
    verifier = Ed25519PublicKey.from_public_bytes(pk[10:])
    verifier.verify(signature[10:], hashlib.blake2b(archive.read_bytes(), digest_size=64).digest())
    verifier.verify(base64.b64decode(lines[3], validate=True), signature[10:] + comment.encode())


@needs_node
@pytest.mark.parametrize(
    ("version", "password", "reason"),
    [
        ("1.260928.57-dev", "correct horse battery", "is not MAJOR.YYMMDD.SEQ"),
        (
            RUNNER_VERSION,
            "wrong password",
            "TAURI_SIGNING_PRIVATE_KEY_PASSWORD does not open TAURI_SIGNING_PRIVATE_KEY",
        ),
    ],
    ids=["not-a-version", "wrong-password"],
)
def test_the_release_signer_refuses_what_it_cannot_sign(tmp_path, version, password, reason):
    secret, _ = _tauri_secret_key("release signer test key", "correct horse battery")
    archive = tmp_path / ARCHIVE
    archive.write_bytes(_archive_bytes())
    result = _sign_release(archive, version, secret, password)
    assert result.returncode == 1
    assert reason in result.stderr
    assert not (tmp_path / f"{ARCHIVE}.minisig").exists()


@needs_node
@needs_bash
def test_an_archive_the_release_signer_signed_installs_for_the_version_it_names(tmp_path, dist):
    # The release format end to end: signed as release-stage.sh signs it, installed
    # by a script the image build wrote the release key into.
    secret, key_line = _tauri_secret_key("release signer test key", "correct horse battery")
    trust = {"keys": [key_line], "timestamp_floor": 1790000000}
    script = _served_script(tmp_path, ".sh", trust)
    staging = tmp_path / "release"
    staging.mkdir()
    archive = staging / ARCHIVE
    archive.write_bytes(_archive_bytes())
    signed = _sign_release(archive, RUNNER_VERSION, secret, "correct horse battery")
    assert signed.returncode == 0, signed.stderr
    dist.files[ARCHIVE] = archive.read_bytes()
    dist.files[ARCHIVE + ".sha256"] = _sha_body(ARCHIVE, dist.files[ARCHIVE], "valid")
    dist.files[ARCHIVE + ".minisig"] = (staging / f"{ARCHIVE}.minisig").read_bytes()
    dist.manifest_runner_version = RUNNER_VERSION
    result = _run(
        tmp_path,
        "--server",
        dist.base,
        "--require-signature",
        extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""},
        script=script,
    )
    out = _output(result)
    assert result.returncode == 0, out
    assert f"Signed with key ID {_key_id(key_line)} for runner {RUNNER_VERSION}." in out
    dist.manifest_runner_version = "1.261001.3"
    result = _run(
        tmp_path,
        "--server",
        dist.base,
        "--require-signature",
        extra_env={"XTM_RUNNER_MINISIGN_PUBKEY": ""},
        script=script,
    )
    out = _output(result)
    assert result.returncode != 0, out
    assert f"is for runner {RUNNER_VERSION}, not 1.261001.3" in out
