"""The relay's install scripts and its distribution stay in step.

The relay installers verify a release archive's minisign signature with the
same code as the runner installers - copied, byte for byte, so the runner's
verifier tests (backend/tests/test_runner_install_scripts.py and
apps/runner/scripts/test-install-verifier.ps1) cover both. They trust the keys
of apps/runner/release-signing.json, which the relay is signed with too, and
name archives the backend serves and the release pipeline builds: a drift in
any of these is a relay that cannot be installed or updated.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.api.v1.relays import _RELAY_ARCHIVES, _RELAY_INSTALL_SCRIPTS

REPO = Path(__file__).resolve().parents[2]
RELAY_SH = REPO / "apps/relay/dist/install.sh"
RELAY_PS1 = REPO / "apps/relay/dist/install.ps1"
RUNNER_SH = REPO / "apps/runner/dist/install.sh"
RUNNER_PS1 = REPO / "apps/runner/dist/install.ps1"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _between(text: str, start: str, end: str) -> str:
    assert start in text and end in text, f"markers {start!r} / {end!r} not found"
    return text[text.index(start) : text.index(end)]


def test_the_shell_verifier_is_the_runners_byte_for_byte():
    start = "# Release signature (minisign). A .minisig is minisign's four-line text:"
    end = "# ── 2. Resolve the version-matched artifact"
    assert _between(_text(RELAY_SH), start, end) == _between(_text(RUNNER_SH), start, end)


def test_the_powershell_verifier_is_the_runners_byte_for_byte():
    start, end = "# BEGIN MINISIGN VERIFIER", "# END MINISIGN VERIFIER"
    assert _between(_text(RELAY_PS1), start, end) == _between(_text(RUNNER_PS1), start, end)


def test_the_scripts_trust_the_release_keys_and_floor():
    trust = json.loads((REPO / "apps/runner/release-signing.json").read_text(encoding="utf-8"))
    keys = " ".join(trust["keys"])
    floor = str(trust.get("timestamp_floor", 0))
    sh, ps1 = _text(RELAY_SH), _text(RELAY_PS1)
    assert f'RELEASE_PUBKEYS="{keys}"' in sh
    assert f"RELEASE_TIMESTAMP_FLOOR={floor}" in sh
    assert f"$releasePubkeys = '{keys}'" in ps1
    assert f"$releaseTimestampFloor = {floor}" in ps1


def test_the_image_build_can_rewrite_their_keys():
    """embed_release_key.py finds exactly one key line and one floor line in each."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "embed_release_key", REPO / "apps/runner/dist/embed_release_key.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    for path in (RELAY_SH, RELAY_PS1):
        suffix = path.suffix
        text = path.read_text(encoding="utf-8")
        assert len(module.KEY_LINES[suffix].findall(text)) == 1, path
        assert len(module.FLOOR_LINES[suffix].findall(text)) == 1, path


def test_the_scripts_ask_for_the_archives_the_backend_serves():
    sh, ps1 = _text(RELAY_SH), _text(RELAY_PS1)
    for (os_key, _arch), archive in _RELAY_ARCHIVES.items():
        if os_key == "windows":
            assert archive.replace("x64", "$arch").replace("arm64", "$arch") in ps1
        else:
            stem = archive if os_key == "macos" else archive.rsplit("-", 1)[0] + "-"
            assert stem.split("$")[0] in sh, archive
    assert "/api/v1/relays/dist/" in sh
    assert "/api/v1/relays/update-manifest" in sh and "/api/v1/relays/update-manifest" in ps1


def test_the_release_builds_every_archive_the_backend_serves():
    # The release workflow (XTM-One-Platform/release) builds the relay with this
    # script, from the commit it releases.
    build = _text(REPO / "apps/relay/scripts/release-build.sh")
    for archive in set(_RELAY_ARCHIVES.values()):
        pattern = re.escape(archive).replace("x64", "(x64|\\$arch)").replace("arm64", "(arm64|\\$arch)")
        assert re.search(pattern, build), f"release-build.sh does not stage {archive}"
    assert 'printf \'%s\' "$version" > "$out/VERSION"' in build


def test_the_platform_image_serves_the_scripts_under_their_stable_names():
    dockerfile = _text(REPO / "Dockerfile")
    for key, name in _RELAY_INSTALL_SCRIPTS:
        source = RELAY_SH if key == "sh" else RELAY_PS1
        assert f"COPY {source.relative_to(REPO).as_posix()} /app/relay_dist/{name}" in dockerfile
        assert f"/app/relay_dist/{name}" in dockerfile.split("embed_release_key.py", 2)[-1]
    assert "COPY relay-dist/ /app/relay_dist/" in dockerfile


def test_the_installers_enroll_with_the_relays_own_flags():
    """`xtm-one-relay enroll` takes --server-url (not the runner's --server)."""
    main_rs = _text(REPO / "apps/relay/src/main.rs")
    assert "server_url: Option<String>" in main_rs
    for text in (_text(RELAY_SH), _text(RELAY_PS1)):
        assert "enroll" in text and "--server-url" in text
        assert "service install" in text
