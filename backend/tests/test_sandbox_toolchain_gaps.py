"""Sandbox toolchain and formatter gaps of large PR 4 (#2160): rust write mode, poetry extras, the uv relock check."""

from pathlib import Path

from app.services.sandbox_workspace import checks
from app.services.sandbox_workspace.detect import RUST_FORMAT_GLOBS, Detection, detect

CHECKS_TOML = Path(__file__).resolve().parents[2] / ".xtm-one" / "checks.toml"


def _detect(files: dict[str, str]) -> Detection:
    return detect(list(files), {path: text.encode() for path, text in files.items()})


def _named(found: Detection, suffix: str) -> checks.CheckSpec:
    return next(spec for name, spec in found.checks.items() if name.endswith(suffix))


def test_rust_format_apply_writes_the_changed_files_with_rustfmt_not_the_whole_workspace() -> None:
    found = _detect({"Cargo.toml": '[package]\nname = "x"\nedition = "2021"\n', "src/main.rs": "fn main() {}\n"})
    apply = _named(found, "rust-format-apply")
    assert apply.argv == ("rustfmt", "--edition", "2021")
    assert "--all" not in apply.argv and "cargo" not in apply.argv
    assert apply.affected == "files" and apply.affected_by_default
    assert apply.affected_globs == RUST_FORMAT_GLOBS
    # The gate stays CI's own command.
    assert _named(found, "rust-format").argv == ("cargo", "fmt", "--all", "--check")


def test_rust_format_apply_reads_the_workspace_edition_and_falls_back_to_cargo_default() -> None:
    workspace = _detect({"Cargo.toml": '[workspace]\nmembers = ["a"]\n[workspace.package]\nedition = "2024"\n'})
    assert _named(workspace, "rust-format-apply").argv[-1] == "2024"
    bare = _detect({"Cargo.toml": '[package]\nname = "x"\n'})
    assert _named(bare, "rust-format-apply").argv[-1] == "2015"


def test_a_poetry_project_with_extras_installs_them() -> None:
    pyproject = '[tool.poetry]\nname = "x"\n[tool.poetry.extras]\nfull = ["pyyaml"]\n'
    found = _detect({"pyproject.toml": pyproject, "poetry.lock": ""})
    assert _named(found, "python-install").argv == ("poetry", "install", "--no-interaction", "--all-extras")


def test_a_poetry_project_without_extras_keeps_the_plain_install() -> None:
    found = _detect({"pyproject.toml": '[tool.poetry]\nname = "x"\n', "poetry.lock": ""})
    assert _named(found, "python-install").argv == ("poetry", "install", "--no-interaction")


def test_backend_lock_update_regenerates_uv_lock_on_toolchain_python() -> None:
    spec = checks.load_checks(CHECKS_TOML.read_text())["backend-lock-update"]
    assert spec.argv == ("uv", "lock")
    assert spec.cwd == "backend" and spec.image == "toolchain-python"
    assert not spec.install_first
