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

"""Write the release keys and signature floor into the runner install scripts.

The install scripts verify the archive's ``.minisig`` against the minisign public
keys they carry, the key id the signature names picking one, and refuse a
signature older than the signature floor they carry. The image build runs this
over the copies it serves (see the Dockerfile), so a script an instance hands
out trusts what its own release trusts: apps/runner/release-signing.json, the
file CI also compiles into every runner (``keys`` and ``timestamp_floor``). The
scripts in the repository carry the same values
(backend/tests/test_runner_install_scripts.py keeps them equal); the rewrite
matters for a build signed with other keys, a fork for instance.

It never fails an image build over the trust file itself: without a usable one
the scripts are left as they are, and say so when they cannot check a
signature. A script whose key or floor line is gone is a repository bug and
fails the build.

Usage: embed_release_key.py <release-signing.json> <script.sh|script.ps1>...
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import sys
from pathlib import Path

# How many release keys a script carries at most: the key in use, the next one
# while a rotation is staged, and room to retire one.
MAX_KEYS = 4

# A minisign key line is the base64 of "Ed" + an 8-byte key id + a 32-byte
# Ed25519 key: 56 characters. A script holds its keys on one line, separated by
# a space, and its floor on another.
_KEYS = r"[A-Za-z0-9+/]{56}(?: [A-Za-z0-9+/]{56}){0," + str(MAX_KEYS - 1) + "}"
KEY_LINES = {
    ".sh": re.compile(r'^(RELEASE_PUBKEYS=")(' + _KEYS + r')(")(\r?)$', re.MULTILINE),
    ".ps1": re.compile(r"^(\$releasePubkeys = ')(" + _KEYS + r")(')(\r?)$", re.MULTILINE),
}
FLOOR_LINES = {
    ".sh": re.compile(r"^(RELEASE_TIMESTAMP_FLOOR=)([0-9]+)()(\r?)$", re.MULTILINE),
    ".ps1": re.compile(r"^(\$releaseTimestampFloor = )([0-9]+)()(\r?)$", re.MULTILINE),
}


def _key_bytes(value: str) -> bytes | None:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == 42 and raw[:2] == b"Ed" else None


def release_trust(path: Path) -> tuple[list[str], int] | str:
    """The key lines and the floor of a release-signing.json, or why it is not usable."""
    try:
        conf = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"cannot read {path}: {exc}"
    if not isinstance(conf, dict):
        return f"{path} is not a JSON object"
    keys = conf.get("keys")
    if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_KEYS:
        return f"{path}: keys must list 1 to {MAX_KEYS} minisign key lines"
    ids = set()
    for key in keys:
        raw = _key_bytes(key) if isinstance(key, str) else None
        if raw is None:
            return f"{path}: {key!r} is not a minisign key line"
        if raw[2:10] in ids:
            return f"{path}: two keys share the key id of {key}"
        ids.add(raw[2:10])
    floor = conf.get("timestamp_floor", 0)
    if isinstance(floor, bool) or not isinstance(floor, int) or floor < 0:
        return f"{path}: timestamp_floor must be a Unix time (0 for none)"
    return keys, floor


def embed(text: str, suffix: str, keys: list[str], floor: int) -> str:
    """``text`` (a script with ``suffix``) carrying ``keys`` and ``floor``.

    ValueError without exactly one key line and one floor line.
    """
    lines = (("release key", KEY_LINES, " ".join(keys)), ("signature floor", FLOOR_LINES, str(floor)))
    for what, patterns, value in lines:
        pattern = patterns[suffix]
        found = len(pattern.findall(text))
        if found != 1:
            raise ValueError(f"expected one {what} line, found {found}")
        text = pattern.sub(lambda m, v=value: m.group(1) + v + m.group(3) + m.group(4), text, count=1)
    return text


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write(__doc__.strip().splitlines()[-1] + "\n")
        return 2
    # Every script is checked first, trust file or not: a script without its
    # key or floor line is a repository bug whatever the trust file holds.
    scripts: list[tuple[Path, str, str]] = []
    for name in argv[2:]:
        path = Path(name)
        suffix = path.suffix.lower()
        if suffix not in KEY_LINES:
            sys.stderr.write(f"error: {path} is neither a .sh nor a .ps1 install script\n")
            return 1
        # newline="" keeps the script's own line endings.
        with path.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
        for what, patterns in (("release key", KEY_LINES), ("signature floor", FLOOR_LINES)):
            found = len(patterns[suffix].findall(text))
            if found != 1:
                sys.stderr.write(f"error: {path}: expected one {what} line, found {found}\n")
                return 1
        scripts.append((path, suffix, text))
    trust = release_trust(Path(argv[1]))
    if isinstance(trust, str):
        sys.stderr.write(f"warning: {trust}; the install scripts keep the keys they carry\n")
        return 0
    keys, floor = trust
    for path, suffix, text in scripts:
        updated = embed(text, suffix, keys, floor)
        if updated != text:
            with path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(updated)
        state = "rewritten" if updated != text else "already current"
        sys.stdout.write(f"{path.name}: release keys and signature floor {state}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
