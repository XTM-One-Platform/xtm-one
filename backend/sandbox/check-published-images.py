#!/usr/bin/env python3
"""Hold the published sandbox images to their build inputs.

published-images.json names, per image family XTM-One-Platform/release
publishes (build-push-sandbox.yml), the version published and the SHA-256 of
the build inputs at that version: the custom-tools images (images=custom-tools,
the top-level version and inputs) and the toolchain images (images=toolchain,
the toolchain object: the five stages of Dockerfile.sandbox.toolchain share
one version and one input hash) and the dev images of sandbox_run_command
(images=dev, the dev object: the python-dev and nodejs-dev stages of
Dockerfile.sandbox.dev, the integration stores of .xtm-one/stores.py baked into
python-dev). This check fails, naming the stale pin:

- when an image's inputs (its Dockerfile, the apt and requirement files of
  its sandbox-images.json entry, the entry itself) no longer hash to the
  manifest: the change would merge without ever being published;
- with --base-manifest, when the inputs changed against the base but the
  version did not: a new version is the publish trigger;
- when the app default of the custom-tools images (_OPENSANDBOX_DEFAULT_IMAGES in
  backend/app/core/config.py) names another version than the manifest.

--print-hashes writes the current hashes, to paste into the manifest with a
new version. Standard library only: CI runs it on a bare runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "published-images.json"
SANDBOX_IMAGES = HERE / "sandbox-images.json"
CONFIG = HERE.parent / "app" / "core" / "config.py"

# The images build-push-sandbox.yml publishes for images=custom-tools, with
# the platform key the app default names them under.
CUSTOM_TOOLS = {"sandbox-python": "python", "sandbox-nodejs": "nodejs"}

# The build inputs of the toolchain images (images=toolchain), paths from the
# repository root: one Dockerfile whose stages are toolchain-python, -node,
# -jvm, -rust and -dbt, the apt files it copies and the stores runner it bakes
# under /opt/stores (a new pin there is refused by an older image's runner).
# Published together under manifest["toolchain"]["version"].
TOOLCHAIN = "sandbox-toolchain"
TOOLCHAIN_INPUTS = (
    "backend/sandbox/Dockerfile.sandbox.toolchain",
    "backend/sandbox/apt.sandbox.toolchain.runtime.txt",
    "backend/sandbox/apt.sandbox.toolchain.rust.txt",
    ".xtm-one/stores.py",
)

# The build inputs of the dev images (images=dev), paths from the repository
# root: the Dockerfile, its apt files and the stores python-dev bakes under
# /opt/stores (a new pin there is refused by the backend-integration check of
# an older image). The locks it installs (backend/uv.lock, frontend/yarn.lock)
# are not inputs: a dependency bump would otherwise need a new image each time.
DEV = "sandbox-dev"
DEV_INPUTS = (
    "backend/sandbox/Dockerfile.sandbox.dev",
    "backend/sandbox/apt.sandbox.dev.txt",
    "backend/sandbox/apt.sandbox.dev.runtime.txt",
    ".xtm-one/stores.py",
)
REPO_ROOT = HERE.parent.parent


def _digest(parts: list[tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for name, data in parts:
        h.update(name.encode() + b"\0" + hashlib.sha256(data).hexdigest().encode() + b"\n")
    return h.hexdigest()


def input_hashes() -> dict[str, str]:
    """The SHA-256 of each custom-tools image's build inputs."""
    entries = {e["image"]: e for e in json.loads(SANDBOX_IMAGES.read_text())}
    out: dict[str, str] = {}
    for image in CUSTOM_TOOLS:
        if image == "sandbox-nodejs":
            parts = [("Dockerfile.sandbox.nodejs", (HERE / "Dockerfile.sandbox.nodejs").read_bytes())]
        else:
            entry = entries[image]
            parts = [("Dockerfile.sandbox.python", (HERE / "Dockerfile.sandbox.python").read_bytes())]
            for key in ("apt_file", "req_file", "runtime_apt_file"):
                parts.append((entry[key], (HERE / entry[key]).read_bytes()))
            # smoke_imports and ci_skip shape CI only, not the image.
            built = {k: v for k, v in entry.items() if k not in ("smoke_imports", "ci_skip")}
            parts.append(("sandbox-images.json", json.dumps(built, sort_keys=True).encode()))
        out[image] = _digest(parts)
    return out


def toolchain_hash() -> str:
    """The SHA-256 of the toolchain images' build inputs."""
    return _digest([(name, (REPO_ROOT / name).read_bytes()) for name in TOOLCHAIN_INPUTS])


def dev_hash() -> str:
    """The SHA-256 of the dev images' build inputs."""
    return _digest([(name, (REPO_ROOT / name).read_bytes()) for name in DEV_INPUTS])


def _families(manifest: dict) -> list[tuple[str, str, dict[str, str], dict[str, str]]]:
    """(family, version, pinned inputs, current inputs) of each published family."""
    toolchain = manifest.get("toolchain") or {}
    dev = manifest.get("dev") or {}
    return [
        ("custom-tools", manifest["version"], manifest["inputs"], input_hashes()),
        ("toolchain", toolchain.get("version", ""), toolchain.get("inputs", {}), {TOOLCHAIN: toolchain_hash()}),
        ("dev", dev.get("version", ""), dev.get("inputs", {}), {DEV: dev_hash()}),
    ]


def app_defaults() -> dict[str, str]:
    m = re.search(r"^_OPENSANDBOX_DEFAULT_IMAGES = (\{.*\})$", CONFIG.read_text(), re.M)
    if not m:
        raise SystemExit(f"FAIL: _OPENSANDBOX_DEFAULT_IMAGES not found in {CONFIG}")
    return json.loads(m.group(1))


def check(manifest: dict, base: dict | None) -> list[str]:
    errors: list[str] = []
    base_families = {f: (v, i) for f, v, i, _ in _families(base)} if base is not None else {}
    for family, version, pinned, current in _families(manifest):
        prefix = "" if family == "custom-tools" else f"{family}."
        stale = False
        for image, digest in current.items():
            if pinned.get(image) != digest:
                stale = True
                tail = "and the app default with it: " if family == "custom-tools" else ""
                errors.append(
                    f"{image}: its build inputs changed and {MANIFEST.name} still pins {prefix}version {version}. "
                    f"Bump {prefix}version, set {prefix}inputs.{image} to {digest} (--print-hashes), "
                    f"{tail}the merge publishes the new version."
                )
        if family in base_families and not stale:
            base_version, base_inputs = base_families[family]
            changed = [i for i in current if base_inputs.get(i) != current[i]]
            if changed and base_version == version:
                errors.append(
                    f"{', '.join(changed)}: build inputs changed against the base without a new version "
                    f"(still {version}); nothing would be published."
                )
    version = manifest["version"]
    defaults = app_defaults()
    for image, key in CUSTOM_TOOLS.items():
        want = f"{image}:{version}"
        if defaults.get(key) != want:
            errors.append(
                f"_OPENSANDBOX_DEFAULT_IMAGES[{key!r}] is {defaults.get(key)!r}, the published version is {want!r}."
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-manifest", type=Path, help="published-images.json of the base branch")
    parser.add_argument("--print-hashes", action="store_true")
    args = parser.parse_args()
    if args.print_hashes:
        hashes = {**input_hashes(), TOOLCHAIN: toolchain_hash(), DEV: dev_hash()}
        sys.stdout.write(json.dumps(hashes, indent=2) + "\n")
        return 0
    manifest = json.loads(MANIFEST.read_text())
    base = None
    if args.base_manifest and args.base_manifest.is_file():
        base = json.loads(args.base_manifest.read_text())
    errors = check(manifest, base)
    for e in errors:
        sys.stdout.write(f"FAIL: {e}\n")
    if not errors:
        sys.stdout.write(
            f"ok: custom-tools images pinned at {manifest['version']}, toolchain images at "
            f"{manifest['toolchain']['version']}, dev images at {manifest['dev']['version']}, "
            f"inputs and app default match\n"
        )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
