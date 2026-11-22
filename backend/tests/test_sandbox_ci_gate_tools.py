"""The CI gate tools of the toolchain images and the checks that run them (#11710 slice 2).

A pull request's CI runs ShellCheck, a betterleaks secret scan, clippy on crates
that link GTK / WebKitGTK, ansible-lint and helm: each one either has a sandbox
check or a tool in the image, so an agent no longer pushes blind. These tests
keep the image, the declared checks and the parity table in step, offline.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from app.services.sandbox_workspace import checks

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "backend" / "sandbox"
DOCKERFILE = SANDBOX / "Dockerfile.sandbox.toolchain"


def _packages(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _declared() -> dict:
    return tomllib.loads((ROOT / ".xtm-one" / "checks.toml").read_text())["check"]


def test_every_toolchain_target_carries_shellcheck():
    assert "shellcheck" in _packages(SANDBOX / "apt.sandbox.toolchain.runtime.txt")


def test_the_downloaded_gate_binaries_are_pinned_by_sha256_per_architecture():
    text = DOCKERFILE.read_text()
    for tool in ("BETTERLEAKS", "HELM"):
        assert re.search(rf"ARG {tool}_VERSION=\d+\.\d+\.\d+", text), tool
        for arch in ("AMD64", "ARM64"):
            assert re.search(rf"ARG {tool}_SHA256_{arch}=[0-9a-f]{{64}}\n", text), (tool, arch)
    # The archive is checked before anything is extracted from it.
    assert "sha256sum -c -" in text
    assert text.index("sha256sum -c -") < text.index("tar -xzf")


def test_ansible_is_pinned_in_toolchain_python_only():
    text = DOCKERFILE.read_text()
    python_stage = text.split("AS toolchain-python", 1)[1].split("\nFROM ", 1)[0]
    assert re.search(r"ARG ANSIBLE_CORE_VERSION=\d+\.\d+\.\d+", python_stage)
    assert re.search(r"ARG ANSIBLE_LINT_VERSION=\d+\.\d+\.\d+", python_stage)
    assert "ansible-lint==${ANSIBLE_LINT_VERSION}" in python_stage


def test_poetry_is_pinned_in_toolchain_python_and_asserted_offline():
    # A Poetry project's detected checks (`poetry install`, `poetry run pytest`) exit 127 without it.
    python_stage = DOCKERFILE.read_text().split("AS toolchain-python", 1)[1].split("\nFROM ", 1)[0]
    assert re.search(r"ARG POETRY_VERSION=\d+\.\d+\.\d+", python_stage)
    assert '"poetry==${POETRY_VERSION}"' in python_stage
    assert "/usr/local/bin/poetry" in python_stage
    images = (ROOT / ".github" / "workflows" / "sandbox-images.yml").read_text()
    line = next(ln for ln in images.splitlines() if ln.strip().startswith("toolchain-python)"))
    assert "poetry --version" in line


def test_poetry_carries_the_pypi_mirror_plugin_the_mirror_env_names():
    # Without it `poetry install` dials pypi.org past the registry mirror (#1250).
    python_stage = DOCKERFILE.read_text().split("AS toolchain-python", 1)[1].split("\nFROM ", 1)[0]
    assert re.search(r"ARG POETRY_PLUGIN_PYPI_MIRROR_VERSION=\d+\.\d+\.\d+", python_stage)
    assert '"poetry-plugin-pypi-mirror==${POETRY_PLUGIN_PYPI_MIRROR_VERSION}"' in python_stage
    mirror = (ROOT / "backend" / "app" / "services" / "sandbox_workspace" / "registry_mirror.py").read_text()
    assert '"POETRY_PYPI_MIRROR_URL": pypi' in mirror


def test_toolchain_rust_installs_the_libraries_runner_ci_installs():
    rust_stage = DOCKERFILE.read_text().split("AS toolchain-rust", 1)[1].split("\nFROM ", 1)[0]
    assert "apt.sandbox.toolchain.rust.txt" in rust_stage
    wanted = set(_packages(SANDBOX / "apt.sandbox.toolchain.rust.txt"))
    workflow = (ROOT / ".github" / "workflows" / "runner-ci.yml").read_text()
    step = workflow.split("Build dependencies (Tauri + xcap + enigo)", 1)[1].split("- name:", 1)[0]
    ci = set(re.findall(r"\b(lib[\w.+-]+-dev|patchelf)\b", step))
    assert ci, "runner-ci.yml no longer names its apt packages in that step"
    assert ci <= wanted, f"runner-ci.yml installs packages toolchain-rust lacks: {sorted(ci - wanted)}"


def test_the_ci_gate_checks_are_declared_on_the_image_that_carries_their_tool():
    declared = _declared()
    expected = {
        "shellcheck": "toolchain-python",
        "secrets": "toolchain-python",
        "runner-clippy": "toolchain-rust",
        "runner-clippy-headless": "toolchain-rust",
        "relay-clippy": "toolchain-rust",
    }
    for name, image in expected.items():
        assert declared[name]["image"] == image, name
        assert declared[name].get("install_first") is False, name
    assert declared["secrets"]["argv"][:3] == ["betterleaks", "dir", "."]
    assert "--redact" in declared["secrets"]["argv"]
    # The whole file still loads through the platform's own parser.
    specs = checks.load_checks((ROOT / ".xtm-one" / "checks.toml").read_text())
    assert {"shellcheck", "secrets", "runner-clippy", "relay-clippy"} <= set(specs)


def test_the_shellcheck_check_covers_every_script_ci_checks():
    argv = " ".join(_declared()["shellcheck"]["argv"])
    workflows = ROOT / ".github" / "workflows"
    for wf, prefix in (
        ("runner-ci.yml", "apps/runner/"),
        ("relay-ci.yml", ""),
        ("release-skill-ci.yml", ""),
        ("ci-license-expiry.yml", ""),
    ):
        for line in (workflows / wf).read_text().splitlines():
            m = re.search(r"\bshellcheck\s+(.*)$", line)
            if not m or ("run:" not in line and not line.strip().startswith("shellcheck")):
                continue
            for script in re.findall(r"[\w./*-]+\.sh", m.group(1)):
                assert f"{prefix}{script}" in argv, (wf, script)


def test_betterleaks_matches_the_version_ci_runs():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    (ci_version,) = set(re.findall(r"ghcr\.io/betterleaks/betterleaks:v(\d+\.\d+\.\d+)", ci))
    (image_version,) = re.findall(r"ARG BETTERLEAKS_VERSION=(\d+\.\d+\.\d+)", DOCKERFILE.read_text())
    assert image_version == ci_version
