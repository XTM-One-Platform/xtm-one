"""The published sandbox image pin as a sandbox check and a pre-push guard.

Gap sandbox-image-pin-not-checked-before-push: an edit to ``.xtm-one/stores.py`` (a build input of the
dev images) was pushed without a version bump in ``backend/sandbox/published-images.json``, and the
"Published image pin" job of sandbox-image-pin.yml went red one CI cycle later: no declared check ran
``check-published-images.py`` and no pre-push guard named it before the commit.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
from pathlib import Path

from app.services.sandbox_workspace import prepush
from app.services.sandbox_workspace.checkout import CheckoutChanges
from app.services.sandbox_workspace.checks import CHECKS_FILE, load_checks

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend" / "sandbox" / "check-published-images.py"


def _spec():
    return load_checks((ROOT / CHECKS_FILE).read_text())["sandbox-image-pin"]


def _script():
    module = importlib.util.spec_from_file_location("check_published_images", SCRIPT)
    assert module is not None and module.loader is not None
    loaded = importlib.util.module_from_spec(module)
    module.loader.exec_module(loaded)
    return loaded


def _inputs(script) -> set[str]:
    """Every file the pin script hashes, as repository paths."""
    sandbox = "backend/sandbox"
    files = {
        f"{sandbox}/Dockerfile.sandbox.python",
        f"{sandbox}/Dockerfile.sandbox.nodejs",
        f"{sandbox}/sandbox-images.json",
    }
    for entry in json.loads(script.SANDBOX_IMAGES.read_text()):
        for key in ("apt_file", "req_file", "runtime_apt_file"):
            if entry.get(key):
                files.add(f"{sandbox}/{entry[key]}")
    files.update(script.TOOLCHAIN_INPUTS)
    files.update(script.DEV_INPUTS)
    return files


def test_the_pin_script_is_a_declared_check_run_as_ci_runs_it() -> None:
    spec = _spec()
    assert spec.argv == ("python", "backend/sandbox/check-published-images.py")
    assert spec.install_first is False
    workflow = (ROOT / ".github" / "workflows" / "sandbox-image-pin.yml").read_text()
    assert "python3 backend/sandbox/check-published-images.py" in workflow


def test_a_stores_edit_names_the_pin_guard_before_the_push() -> None:
    """The failure of the gap: a change to .xtm-one/stores.py alone, no check run, must be named."""
    spec = _spec()
    changes = CheckoutChanges(modified=(".xtm-one/stores.py",), added=(), removed=())
    assert prepush.touched(changes, spec) == [".xtm-one/stores.py"]


def test_the_guard_globs_cover_every_input_the_pin_script_hashes() -> None:
    spec = _spec()
    script = _script()
    inputs = _inputs(script)
    assert ".xtm-one/stores.py" in inputs
    uncovered = sorted(
        p for p in inputs if not prepush.touched(CheckoutChanges(modified=(p,), added=(), removed=()), spec)
    )
    assert uncovered == []
    # The manifest itself is guarded too: a version bump without the matching hashes is caught before the push.
    assert any(fnmatch.fnmatchcase("backend/sandbox/published-images.json", g) for g in spec.pre_push)


def test_a_change_outside_the_image_inputs_names_no_pin_guard() -> None:
    spec = _spec()
    changes = CheckoutChanges(
        modified=("backend/app/services/agent.py", "backend/sandbox/SANDBOX.md", "backend/sandbox/build-image.sh"),
        added=(),
        removed=(),
    )
    assert prepush.touched(changes, spec) == []


def test_the_guard_reads_every_file_the_pin_script_needs_on_a_narrow_checkout() -> None:
    needs = set(_spec().needs)
    assert {"backend/sandbox", "backend/app/core/config.py", ".xtm-one/stores.py"} <= needs
