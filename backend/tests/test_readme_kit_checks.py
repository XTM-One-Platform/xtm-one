"""The README kit has declared sandbox checks, so a README or an image is never built through code_interpreter."""

from __future__ import annotations

from pathlib import Path

from app.services.sandbox_workspace.checks import load_checks

REPO_ROOT = Path(__file__).resolve().parents[2]
KIT_CHECKS = {
    "readme-kit-check": "readme.py",
    "readme-kit-render": "readme.py",
    "readme-kit-contrast": "contrast.py",
    "readme-kit-frame": "frame.py",
    "readme-kit-walkthrough": "walkthrough.py",
}


def test_every_readme_kit_script_has_a_declared_check_on_python_dev() -> None:
    specs = load_checks((REPO_ROOT / ".xtm-one" / "checks.toml").read_text(encoding="utf-8"))
    for name, script in KIT_CHECKS.items():
        spec = specs[name]
        # Run from the repository root, as the scripts' usage lines say; the image carries Pillow and fontTools.
        assert spec.argv[:2] == ("python", f".github/scripts/readme-kit/{script}"), name
        assert spec.image == "python-dev", name
        assert (REPO_ROOT / ".github" / "scripts" / "readme-kit" / script).is_file(), script


def test_the_gate_check_covers_the_five_products_by_default() -> None:
    spec = load_checks((REPO_ROOT / ".xtm-one" / "checks.toml").read_text(encoding="utf-8"))["readme-kit-check"]
    assert spec.argv[-1] == "--check"
    data = REPO_ROOT / ".github" / "scripts" / "readme-kit" / "readme_data"
    for key in spec.default_args:
        assert (data / f"{key.replace('-', '_')}.py").is_file(), key
