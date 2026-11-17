"""apps/generate-manifests.sh names the Office add-in as the platform names itself.

The script reads PLATFORM_MODE and PLATFORM_NAME from the environment, as the
platform does, and must land on the name the platform's branding resolves for
the same values: ``_env_name()`` first, then ``mode_default_name()``. Run on a
scratch copy, so the committed manifests are never rewritten by the test.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.config import PLATFORM_MODES
from app.services.branding import _env_name, mode_default_name

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "apps" / "generate-manifests.sh"
MANIFEST_DIR = REPO_ROOT / "apps" / "microsoft-office" / "manifest"
GENERATED = ("manifest.prod.xml", "manifest.outlook.xml", "manifest.xml")


def _generate(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    apps = tmp_path / "apps"
    manifests = apps / "microsoft-office" / "manifest"
    manifests.mkdir(parents=True)
    shutil.copy(SCRIPT, apps / SCRIPT.name)
    for template in MANIFEST_DIR.glob("*.template.xml"):
        shutil.copy(template, manifests / template.name)
    return subprocess.run(  # noqa: S603 - the repository's own script, on a scratch copy
        ["bash", str(apps / SCRIPT.name)],  # noqa: S607 - bash, as the README runs it
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        check=False,
    )


def _display_name(tmp_path: Path) -> str:
    text = (tmp_path / "apps/microsoft-office/manifest/manifest.prod.xml").read_text(encoding="utf-8")
    match = re.search(r'<DisplayName DefaultValue="([^"]*)"', text)
    assert match, "manifest.prod.xml has no DisplayName"
    return match.group(1)


# Every mode the platform accepts, then values it normalises or refuses: each
# goes through the platform's own validator, never a copy of it.
MODE_VALUES = [None, *PLATFORM_MODES, " Copilot\r", "DEV", "", "ai_disable", "xtm one", "copilot-like"]


@pytest.mark.parametrize("platform_mode", MODE_VALUES)
def test_the_script_reads_platform_mode_as_the_platform_does(platform_mode, tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.core.config import settings as app_settings

    result = _generate(tmp_path, **({} if platform_mode is None else {"PLATFORM_MODE": platform_mode}))

    try:
        mode = Settings._validate_platform_mode(platform_mode or "")
    except ValueError:
        assert result.returncode != 0, f"the platform refuses {platform_mode!r}, the script took it"
        assert "PLATFORM_MODE must be one of" in result.stderr
        assert not (tmp_path / "apps/microsoft-office/manifest/manifest.prod.xml").exists()
        return

    assert result.returncode == 0, f"the platform accepts {platform_mode!r}: {result.stderr}"
    monkeypatch.setattr(app_settings, "platform_mode", mode)
    assert _display_name(tmp_path) == mode_default_name()


@pytest.mark.parametrize("platform_name", ["Acme AI", " Acme AI \r", "   "])
def test_platform_name_wins_trimmed_and_a_blank_one_is_unset(platform_name, tmp_path, monkeypatch):
    from app.core.config import settings as app_settings

    result = _generate(tmp_path, PLATFORM_NAME=platform_name, PLATFORM_MODE="copilot")
    assert result.returncode == 0, result.stderr

    monkeypatch.setattr(app_settings, "platform_name", platform_name)
    monkeypatch.setattr(app_settings, "platform_mode", "copilot")
    assert _display_name(tmp_path) == (_env_name() or mode_default_name())


def test_the_committed_manifests_are_the_default_generation(tmp_path):
    result = _generate(tmp_path)
    assert result.returncode == 0, result.stderr
    for name in GENERATED:
        fresh = (tmp_path / "apps/microsoft-office/manifest" / name).read_text(encoding="utf-8")
        assert (MANIFEST_DIR / name).read_text(encoding="utf-8") == fresh, (
            f"{name} is stale: run ./apps/generate-manifests.sh with no PLATFORM_MODE or PLATFORM_NAME"
        )
