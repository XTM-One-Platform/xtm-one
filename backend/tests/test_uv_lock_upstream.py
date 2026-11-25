"""The committed backend/uv.lock names PyPI only, and a lock resolved through the sandbox mirror is mapped back (#2360)."""

import re
from pathlib import Path

import pytest

from scripts import lock_upstream_urls as up

LOCK = Path(__file__).resolve().parents[1] / "uv.lock"
MIRROR = "http://copilot-registry-mirror.copilot.svc.cluster.local:8080"
#: The lock revision uv 0.11 writes; a lower one is a lock written by an older uv or a stripped one.
MIN_REVISION = 5


def test_the_committed_lock_names_no_cluster_host() -> None:
    text = LOCK.read_text(encoding="utf-8")
    for needle in ("svc.cluster.local", "copilot-registry-mirror", "/pypi/files/"):
        assert needle not in text, f"backend/uv.lock names {needle!r}: run backend-lock-update again (#2360)"
    hosts = set(re.findall(r'(?:registry|url) = "(https?://[^/"]+)', text))
    assert hosts <= {"https://pypi.org", "https://files.pythonhosted.org"}, hosts


def test_the_committed_lock_keeps_the_revision() -> None:
    match = re.search(r"^revision = (\d+)$", LOCK.read_text(encoding="utf-8"), re.MULTILINE)
    assert match and int(match.group(1)) >= MIN_REVISION


@pytest.mark.parametrize("index", [f"{MIRROR}/pypi/simple/", f"{MIRROR}/pypi/simple"])
def test_mirror_base_reads_the_index_the_sandbox_sets(index: str) -> None:
    assert up.mirror_base(index) == MIRROR


@pytest.mark.parametrize("index", ["", "https://pypi.org/simple", "https://example.com/simple/"])
def test_mirror_base_leaves_any_other_index(index: str) -> None:
    assert up.mirror_base(index) is None


def test_upstream_maps_the_mirror_urls_and_keeps_the_hashes() -> None:
    lock = (
        "version = 1\nrevision = 3\n"
        f'source = {{ registry = "{MIRROR}/pypi/simple/" }}\n'
        f'sdist = {{ url = "{MIRROR}/pypi/files/packages/aa/bb/x-1.0.tar.gz", hash = "sha256:00" }}\n'
    )
    out = up.upstream(lock, MIRROR, 5)
    assert MIRROR not in out
    assert 'registry = "https://pypi.org/simple"' in out
    assert 'url = "https://files.pythonhosted.org/packages/aa/bb/x-1.0.tar.gz", hash = "sha256:00"' in out
    assert "\nrevision = 5\n" in out


def test_upstream_never_lowers_the_revision_and_is_a_no_op_without_a_mirror() -> None:
    lock = 'version = 1\nrevision = 6\nsource = { registry = "https://pypi.org/simple" }\n'
    assert up.upstream(lock, None, 5) == lock
    assert up.upstream(lock, MIRROR, None) == lock


def test_main_writes_the_file_and_fails_when_the_mirror_is_left(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(f'version = 1\nrevision = 3\nsource = {{ registry = "{MIRROR}/pypi/simple/" }}\n', encoding="utf-8")
    monkeypatch.setenv("UV_DEFAULT_INDEX", f"{MIRROR}/pypi/simple/")
    assert up.main(["x", str(lock), "5"]) == 0
    assert lock.read_text(encoding="utf-8") == 'version = 1\nrevision = 5\nsource = { registry = "https://pypi.org/simple" }\n'
    lock.write_text(f'note = "{MIRROR}/other"\n', encoding="utf-8")
    assert up.main(["x", str(lock)]) == 1
