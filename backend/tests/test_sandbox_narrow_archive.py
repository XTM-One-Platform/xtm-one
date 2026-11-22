"""``narrow_archive``: a repository archive over the in-memory limit, narrowed to a checkout's sub_paths (#414)."""

from __future__ import annotations

import hashlib
import io
import tarfile

import pytest

from app.services.sandbox_workspace.repository import ArchiveTooLargeError, extract_tree, narrow_archive

ROOT = "acme-site-0a1b2c3"


def _archive(files: dict[str, bytes]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        top = tarfile.TarInfo(ROOT)
        top.type = tarfile.DIRTYPE
        tf.addfile(top)
        for name, data in files.items():
            info = tarfile.TarInfo(f"{ROOT}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def _names(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        return sorted(m.name for m in tf.getmembers() if m.isfile())


SITE = {
    "package.json": b'{"name": "site"}',
    "pnpm-lock.yaml": b"lockfileVersion: 9\n",
    ".github/workflows/ci.yml": b"on: push\n",
    "src/app.ts": b"export {}\n",
    "src/logo.svg": b"<svg/>",
    "src/hero.png": b"\x89PNG" + b"0" * 64,
    "scripts/seed.ts": b"export {}\n",
    "public/video.mp4": b"0" * 4096,
    "public/photo.jpg": b"0" * 2048,
    "public/robots.txt": b"User-agent: *\n",
    "docs/big.json": b"0" * 1024,
}


def test_narrow_keeps_every_file_under_the_paths_and_the_small_text_files_outside():
    narrowed = narrow_archive(_archive(SITE), ["src", "scripts"], max_bytes=1 << 20, outside_max_bytes=512)
    assert _names(narrowed.data) == sorted(
        f"{ROOT}/{p}"
        for p in (
            "package.json",
            "pnpm-lock.yaml",
            ".github/workflows/ci.yml",
            "src/app.ts",
            "src/logo.svg",
            "src/hero.png",
            "scripts/seed.ts",
            "public/robots.txt",
        )
    ), "a media file under a sub_path is kept; the top directory is kept in every name"
    assert sorted(narrowed.left_out) == ["docs/big.json", "public/photo.jpg", "public/video.mp4"]
    assert narrowed.left_out_bytes == 4096 + 2048 + 1024
    assert (narrowed.kept_under, narrowed.kept_outside) == (4, 4)


def test_the_narrowed_archive_extracts_to_the_same_checkout_under_the_paths():
    whole = extract_tree(_archive(SITE).getvalue(), sub_paths=["src"], max_bytes=1 << 20)
    narrowed = narrow_archive(_archive(SITE), ["src"], max_bytes=1 << 20)
    tree = extract_tree(narrowed.data, sub_paths=["src"], max_bytes=1 << 20)
    assert tree.root == whole.root == ROOT, "the commit the record names comes from the top directory"
    assert sorted(tree.files) == sorted(whole.files)
    assert sorted(tree.companions) == sorted(whole.companions)


def test_the_cap_applies_to_the_selected_bytes_and_names_the_paths():
    files = {"src/a.ts": b"0" * 600, "src/b.ts": b"0" * 600, "public/v.mp4": b"0" * 10_000}
    assert narrow_archive(_archive(files), ["src"], max_bytes=2000).left_out == ["public/v.mp4"]
    with pytest.raises(ArchiveTooLargeError, match=r"files under src \(0\.0 MB\) and the detection files outside"):
        narrow_archive(_archive(files), ["src"], max_bytes=1000)


def test_the_in_memory_bound_is_the_compressed_output_not_the_selected_bytes() -> None:
    """materialize-archive-size-cap: a selection over the in-memory limit that compresses under it is kept."""
    files = {"package.json": b"{}", **{f"seed/data-{i}.json": b"0" * 50_000 for i in range(4)}}
    narrowed = narrow_archive(_archive(files), ["seed"], max_bytes=1 << 20, max_output_bytes=50_000)
    assert narrowed.kept_under == 4, "200 KB of selected text compresses far under the 50 KB output bound"
    assert len(narrowed.data) <= 50_000
    noise = {f"seed/blob-{i}.bin": hashlib.shake_256(bytes([i])).digest(100_000) for i in range(4)}
    with pytest.raises(ArchiveTooLargeError, match=r"narrowed archive of seed is over 0 MB compressed"):
        narrow_archive(_archive(noise), ["seed"], max_bytes=1 << 20, max_output_bytes=1_000)


def test_the_narrowed_bound_is_the_workspace_quota_not_a_fixed_in_memory_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """materialize-archive-size-cap, retest FAIL of #1249: 338.7 MB of images under one sub_path compress to about
    their own size, over the former 256 MB cap; the workspace quota (2 GB by default) is what bounds them now."""
    from app.core.config import settings
    from app.integrations.providers.github import contents

    mb = 1024 * 1024
    monkeypatch.setattr(settings, "code_interpreter_workspace_quota_mb", 2048)
    assert contents._narrowed_archive_bound() == 2048 * mb > 339 * mb
    monkeypatch.setattr(settings, "code_interpreter_workspace_quota_mb", 8192)
    assert contents._narrowed_archive_bound() == contents._MAX_NARROWED_SELECTED_BYTES, "capped at the runaway bound"
    monkeypatch.setattr(settings, "code_interpreter_workspace_quota_mb", 64)
    assert contents._narrowed_archive_bound() == contents._MAX_ARCHIVE_BYTES, "never below the whole-archive bound"


def test_small_files_outside_the_paths_past_their_budget_are_left_out_never_refused() -> None:
    """#432: a site whose seed data and pages outside the sub_path pass the archive limit still narrows."""
    files = {
        "package.json": b'{"name": "site"}',
        "pnpm-lock.yaml": b"lockfileVersion: 9\n",
        ".github/workflows/ci.yml": b"on: push\n",
        ".xtm-one/checks.toml": b"",
        "tsconfig.json": b"{}",
        "src/app.ts": b"export {}\n",
        **{f"seed/data-{i}.json": b"0" * 300 for i in range(10)},
        "public/logo.svg": b"<svg/>",
    }
    narrowed = narrow_archive(_archive(files), ["src"], max_bytes=2000, outside_max_bytes=512, outside_budget_bytes=600)
    kept = {name.split("/", 1)[1] for name in _names(narrowed.data)}
    assert {
        "package.json",
        "pnpm-lock.yaml",
        ".github/workflows/ci.yml",
        ".xtm-one/checks.toml",
        "tsconfig.json",
    } <= kept
    assert "src/app.ts" in kept
    assert len([p for p in kept if p.startswith("seed/")]) == 2, "the other small files stop at their budget"
    assert "public/logo.svg" in narrowed.left_out, "an SVG outside the paths is media"
    assert len([p for p in narrowed.left_out if p.startswith("seed/")]) == 8


def test_the_detection_files_outside_the_paths_are_kept_past_the_budget() -> None:
    files = {"README.md": b"0" * 400, "package.json": b"{}", "src/a.ts": b"a"}
    narrowed = narrow_archive(_archive(files), ["src"], max_bytes=1 << 20, outside_budget_bytes=100)
    assert narrowed.left_out == ["README.md"]
    assert f"{ROOT}/package.json" in _names(narrowed.data)


def test_git_metadata_and_links_are_never_kept():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        for name, data in ((".git/config", b"[remote] url = https://x:token@h/r"), ("src/a.ts", b"a")):
            info = tarfile.TarInfo(f"{ROOT}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(f"{ROOT}/src/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    buffer.seek(0)
    assert _names(narrow_archive(buffer, ["src"], max_bytes=1 << 20).data) == [f"{ROOT}/src/a.ts"]


def test_a_stream_that_is_not_a_tar_archive_is_refused():
    with pytest.raises(ArchiveTooLargeError, match="could not be read as a tar archive"):
        narrow_archive(io.BytesIO(b"not an archive at all"), ["src"], max_bytes=1 << 20)
