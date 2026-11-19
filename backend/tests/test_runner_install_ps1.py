"""Behaviour of ``apps/runner/dist/install.ps1``, run under PowerShell 7 on a POSIX host.

The install.sh tests, the shared harness (the stand-in platform, the signing
keys) and what both scripts pin are in ``tests/test_runner_install_scripts.py``.
The two files share one xdist group: a job runs them on one worker, as one
module ran before (install.ps1 stops every process named xtm-one-runner on the
host, the install.sh tests run stand-ins by that name). Being two files, two
jobs can run them at once, on two hosts.
"""

from __future__ import annotations

import base64
import functools
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests import test_runner_install_scripts as _scripts

DIST_DIR = _scripts.DIST_DIR
DIST_PREFIX = _scripts.DIST_PREFIX
INSTALL_PS1 = _scripts.INSTALL_PS1
KEY = _scripts.KEY
MANIFEST_PATH = _scripts.MANIFEST_PATH
OTHER_KEY = _scripts.OTHER_KEY
RUNNER_VERSION = _scripts.RUNNER_VERSION
SIGN_IN_PAGE = _scripts.SIGN_IN_PAGE
TAURI_SIGNED = _scripts.TAURI_SIGNED
TAURI_SIGNER = _scripts.TAURI_SIGNER
THIRD_KEY = _scripts.THIRD_KEY
WIN_ARCHIVE = _scripts.WIN_ARCHIVE
_NOT_A_SIGNATURE = _scripts._NOT_A_SIGNATURE
_NOT_THIS_RELEASE = _scripts._NOT_THIS_RELEASE
_OVERRIDE_NOTICE = _scripts._OVERRIDE_NOTICE
_SIGNED_FORMS = _scripts._SIGNED_FORMS
_SMALL_ORDER_KEYS = _scripts._SMALL_ORDER_KEYS
_TAURI_SIGNER_ID = _scripts._TAURI_SIGNER_ID
_key_id = _scripts._key_id
_mirror = _scripts._mirror
_names_no_file = _scripts._names_no_file
_output = _scripts._output
_root = _scripts._root
_run_embed = _scripts._run_embed
_served_script = _scripts._served_script
_sha_body = _scripts._sha_body
_small_order_forgery = _scripts._small_order_forgery
_stage_signed = _scripts._stage_signed
_stage_windows = _scripts._stage_windows
_trust = _scripts._trust
_unusable_key = _scripts._unusable_key
_versioned = _scripts._versioned
dist = _scripts.dist

pytestmark = pytest.mark.xdist_group("runner_install_scripts")

# Never on Windows: install.ps1 stops a running xtm-one-runner process before
# replacing it, which on a developer's machine would be their real runner.
PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(
    sys.platform == "win32" or PWSH is None,
    reason="install.ps1 is exercised with PowerShell 7 on a POSIX CI host",
)


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
