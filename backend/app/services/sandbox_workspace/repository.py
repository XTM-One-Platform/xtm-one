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

"""``materialize_repository``: a repository's tree checked out into the workspace, server-side.

The archive comes from the provider's own ``repository_archive`` tool (the
GitHub integration's ``download_github_repository_archive``), called through
the run's dispatcher so the integration's credential, its repository scope
and the approval gate apply as for any call of it; the sandbox stays offline
and never sees the credential. What is written is the TREE, never a clone:
no ``.git`` directory reaches the workspace (an archive that carries one has
it dropped, ``.git/config`` with whatever credential its remote URL holds
included), nor a link or anything but a regular file.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import IO

from app.services.sandbox_workspace.actions_lint import ACTIONLINT_CONFIGS
from app.services.sandbox_workspace.checks import CHECKS_FILE, ChecksError, CheckSpec, load_checks
from app.services.sandbox_workspace.detect import (
    MAX_REQUIREMENTS_BYTES,
    WORKSPACE_ROOT_FILES,
    Widening,
    check_needs,
    check_support_files,
    command_named_files,
    install_lifecycle_paths,
    is_companion_name,
    is_lint_config,
    is_referencing_config,
    is_referencing_file,
    is_relay_source,
    is_requirements,
    is_workflow,
    lint_config_member_dirs,
    maven_reactor_paths,
    package_local_files,
    referenced_files,
    relay_source_dirs,
    release_note_dirs,
    subdirectory_dependencies,
    widen_to_check_dirs,
    widen_to_package_roots,
    workspace_dependency_dirs,
    workspace_member_manifests,
)
from app.services.sandbox_workspace.detect import companions as companion_files
from app.services.sandbox_workspace.paths import WorkspacePathError, is_under, normalize


@dataclass
class ExtractedTree:
    files: list[tuple[str, bytes]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: The archive's top directory, dropped from every path (GitHub's ``<owner>-<repo>-<sha>/``).
    root: str = ""
    #: The files kept outside *sub_paths* so the checkout's checks are detected (``detect.companions``).
    companions: list[str] = field(default_factory=list)
    #: The sub_paths written, after :func:`detect.widen_to_check_dirs`; empty without sub_paths.
    sub_paths: list[str] = field(default_factory=list)
    #: The sub_paths widened to the directory a declared check runs from.
    widened: list[Widening] = field(default_factory=list)
    #: The repository directories added to the sub_paths because a requirements file under them installs
    #: from one (``#subdirectory=``, ``detect.subdirectory_dependencies``).
    dependencies: list[str] = field(default_factory=list)
    #: The Maven reactor modules added to the sub_paths because a POM under them depends on their artifact
    #: (``detect.maven_reactor_paths``): a module builds against its siblings, which no repository publishes.
    reactor: list[str] = field(default_factory=list)
    #: The repository directories added to the sub_paths because an install lifecycle script of a workspaces
    #: root above them builds there (``cd packages/core && tsup``, ``detect.install_lifecycle_paths``).
    lifecycle: list[str] = field(default_factory=list)
    #: The workspace member directories added to the sub_paths because a package under them links it by the
    #: ``workspace:`` protocol (``detect.workspace_dependency_dirs``) or an ESLint configuration in or above them
    #: imports by name (``detect.lint_config_member_dirs``): its checks import the sibling's source.
    workspace_links: list[str] = field(default_factory=list)
    #: The Relay ``src`` directories added to the sub_paths because a ``relay.config.*`` in or above them names
    #: it (``detect.relay_source_dirs``): the compiler reads every fragment there.
    relay_sources: list[str] = field(default_factory=list)
    #: The repository paths added to the sub_paths because a declared check of a directory they hold
    #: reads them (``needs`` of ``[check.NAME]``, ``detect.check_needs``).
    needed: list[str] = field(default_factory=list)
    #: The files outside the sub_paths that a test or a TypeScript config under them names by a relative
    #: path (a backend fixture a frontend test reads, ``detect.referenced_files``), kept as companions.
    referenced: list[str] = field(default_factory=list)
    #: The files outside the sub_paths a workflow step or a ``package.json`` script of the checkout names
    #: (``-r ci-requirements.txt``, ``node ../scripts/x.mjs``, ``detect.command_named_files``), kept as companions.
    named: list[str] = field(default_factory=list)
    #: The release-note directories added to the sub_paths (``.changeset``, ``detect.release_note_dirs``): a note
    #: written there belongs to the change, so the record covers it and ``from_checkout`` commits it.
    release_notes: list[str] = field(default_factory=list)
    #: The files outside the sub_paths a ``package.json`` or ``.yarnrc.yml`` of the install names by a local path
    #: (``patch:x@1#./patch/x.patch``, ``file:``, ``portal:``, ``yarnPath``, ``detect.package_local_files``), kept as
    #: companions so the install finds them.
    local: list[str] = field(default_factory=list)


class ArchiveTooLargeError(ValueError):
    pass


#: A checks file larger than this is not read to widen sub_paths (a declaration is a few KB).
_MAX_CHECKS_FILE_BYTES = 256 * 1024


@dataclass(frozen=True)
class _Member:
    name: str
    is_file: bool
    is_dir: bool
    size: int


def _clean(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name.lstrip("/")


def _members(data: bytes) -> Iterator[tuple[_Member, Callable[[], bytes]]]:
    """Every member of a tar (any compression) or zip archive, with a reader valid until the next one."""
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                mode = (info.external_attr >> 16) & 0o170000
                is_file = not info.is_dir() and mode in (0, 0o100000)
                yield _Member(info.filename, is_file, info.is_dir(), info.file_size), (lambda i=info: archive.read(i))
        return
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for info in archive.getmembers():

            def _read(i: tarfile.TarInfo = info) -> bytes:
                handle = archive.extractfile(i)
                return handle.read() if handle is not None else b""

            yield _Member(info.name, info.isfile(), info.isdir(), info.size), _read


def _relative(name: str, root: str) -> str:
    """*name* below the archive's top directory, normalized, or ``""`` when it is not a file the tree keeps."""
    if root:
        if not name.startswith(root + "/"):
            return ""
        name = name[len(root) + 1 :]
    name = name.rstrip("/")
    if not name or ".git" in name.split("/"):
        return ""
    try:
        return normalize(name)
    except WorkspacePathError:
        return ""


def _parents(path: str) -> list[str]:
    """Every directory above *path* (``a/b/c.txt``: ``a``, ``a/b``)."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def _declared_checks(data: bytes, root: str) -> dict[str, CheckSpec]:
    """The checks the archive's ``.xtm-one/checks.toml`` declares, or none (absent, too large or invalid)."""
    for member, read in _members(data):
        if member.is_file and _relative(_clean(member.name), root) == CHECKS_FILE:
            if member.size > _MAX_CHECKS_FILE_BYTES:
                return {}
            try:
                return load_checks(read().decode("utf-8", errors="replace"))
            except ChecksError:
                return {}
    return {}


def extract_tree(
    data: bytes,
    *,
    sub_paths: list[str] | None = None,
    max_bytes: int,
    extra_files: list[str] | None = None,
    exact_files: bool = False,
) -> ExtractedTree:
    """The regular files of an archive, its top directory dropped, filtered to *sub_paths*.

    With *sub_paths*, a sub_path inside the directory a check declared in
    the archive's ``.xtm-one/checks.toml`` runs from is widened to that
    directory first (``detect.widen_to_check_dirs``: the check imports
    what lies there), and the companion files their checks need (the
    manifests and lock files above them, the workflows, the checks file:
    ``detect.companions``; the scripts a declared check's command names,
    ``../.xtm-one/stores.py``: ``detect.check_support_files``; the
    ``package.json`` of every member of the JavaScript workspaces a sub_path
    sits in, so a frozen-lockfile install resolves: ``detect.workspace_member_manifests``;
    the files the root's install lifecycle scripts copy, ``detect.install_lifecycle_paths``;
    the sibling members a package under the sub_paths links by ``workspace:``, added to the sub_paths and
    listed in ``workspace_links``, ``detect.workspace_dependency_dirs``;
    the files a test or a TypeScript config under the sub_paths names by a relative path,
    ``detect.referenced_files``) are kept too and listed in ``companions``. The directories those scripts
    build in (``prepare``: ``cd packages/core && tsup``) are added to the
    sub_paths and listed in ``lifecycle``.
    *extra_files* are repository files kept as they are, outside *sub_paths*, never widened nor listed as
    companions: the files a branch changes outside a checkout, read by ``sandbox_checkout_rebase``.
    With *exact_files*, a sub_path naming a regular file of the archive is kept as that file, never widened to
    the directory a check runs from nor to its package root: ``materialize_repository(add_paths=[a file])``
    grows a checkout whose checks were already set up by its first tree, so one file asked is one file written.
    Raises :class:`ArchiveTooLargeError` past *max_bytes* of selected content.
    """
    members = [
        (_clean(member.name), member)
        for member, _ in _members(data)
        if _clean(member.name) and _clean(member.name) != "pax_global_header"
    ]
    tops = {name.split("/", 1)[0] for name, _member in members}
    root = next(iter(tops)) if len(tops) == 1 and any("/" in name for name, _member in members) else ""
    wanted = [normalize(sub) for sub in sub_paths or [] if str(sub).strip().strip("/")]
    tree = ExtractedTree(root=root)
    if wanted:
        declared = _declared_checks(data, root)
        sizes: dict[str, int] = {}
        for name, member in members:
            rel = _relative(name, root)
            if rel and member.is_file:
                sizes[rel] = member.size
        exact = [sub for sub in wanted if sub in sizes] if exact_files else []
        wanted = [sub for sub in wanted if sub not in exact]
        wanted, tree.widened = widen_to_check_dirs(wanted, declared)
        # A sub_path inside a JavaScript package no declared check covers takes the package root: its unit
        # and type checks load the package's own config and assets (front/config.ts beside front/src).
        wanted, by_package = widen_to_package_roots(wanted, sizes)
        tree.widened = [*tree.widened, *by_package]
        wanted = [*wanted, *(sub for sub in exact if not any(is_under(sub, other) for other in wanted))]
        tree.needed = check_needs(wanted, declared)
        wanted = [*wanted, *tree.needed]
        tree.sub_paths = list(wanted)
        requirements: dict[str, bytes] = {}
        workspace_roots: dict[str, bytes] = {}
        referencing: dict[str, bytes] = {}
        workflows: dict[str, bytes] = {}
        manifests: dict[str, bytes] = {}
        # Every other package.json (a workspace member's) and the .yarnrc.yml above a sub_path: their local
        # references (patch:, file:, yarnPath) are read by the install too.
        others: dict[str, bytes] = {}
        yarnrcs: dict[str, bytes] = {}
        poms: dict[str, bytes] = {}
        # The ESLint configurations in or above a sub_path: a bare import of a workspace member's name links it.
        lint_configs: dict[str, bytes] = {}
        above = {directory for sub in wanted for directory in (["", *_parents(sub + "/x")])}
        for member, read in _members(data):
            path = _relative(_clean(member.name), root) if member.is_file else ""
            if path and member.size <= MAX_REQUIREMENTS_BYTES and path.rsplit("/", 1)[-1] == "pom.xml":
                poms[path] = read()
            if (
                path
                and member.size <= MAX_REQUIREMENTS_BYTES
                and is_lint_config(path)
                and (path.rpartition("/")[0] in above or any(is_under(path, sub) for sub in wanted))
            ):
                lint_configs[path] = read()
            if path and member.size <= MAX_REQUIREMENTS_BYTES and is_workflow(path):
                workflows[path] = read()
            elif (
                path
                and member.size <= MAX_REQUIREMENTS_BYTES
                and is_requirements(path)
                and any(is_under(path, sub) for sub in wanted)
            ):
                requirements[path] = read()
            elif (
                path
                and member.size <= MAX_REQUIREMENTS_BYTES
                and is_referencing_file(path)
                and (
                    any(is_under(path, sub) for sub in wanted)
                    # A tool configuration above a sub_path (relay.config.json, tsconfig.json of the
                    # project the sub_path is a part of): the checks of the sub_path read it.
                    or (is_referencing_config(path) and path.rpartition("/")[0] in above)
                )
            ):
                referencing[path] = read()
            elif (
                path
                and member.size <= MAX_REQUIREMENTS_BYTES
                and path.rsplit("/", 1)[-1] in WORKSPACE_ROOT_FILES
                and path.rpartition("/")[0] in above
            ):
                workspace_roots[path] = read()
            elif (
                path
                and member.size <= MAX_REQUIREMENTS_BYTES
                and path.rsplit("/", 1)[-1] == "package.json"
                and any(is_under(path, sub) for sub in wanted)
                and "node_modules" not in path.split("/")
            ):
                manifests[path] = read()
            elif (
                path
                and member.size <= MAX_REQUIREMENTS_BYTES
                and path.rsplit("/", 1)[-1] == ".yarnrc.yml"
                and path.rpartition("/")[0] in above
            ):
                yarnrcs[path] = read()
            elif (
                path
                and member.size <= MAX_REQUIREMENTS_BYTES
                and path.rsplit("/", 1)[-1] == "package.json"
                and "node_modules" not in path.split("/")
            ):
                others[path] = read()
        directories = {d for path in sizes for d in _parents(path)}
        tree.dependencies = subdirectory_dependencies(requirements, directories, wanted)
        wanted = [*wanted, *tree.dependencies]
        tree.reactor, module_poms = maven_reactor_paths(poms, wanted)
        wanted = [*wanted, *tree.reactor]
        tree.lifecycle, lifecycle_files = install_lifecycle_paths(workspace_roots, sizes, wanted)
        wanted = [*wanted, *tree.lifecycle]
        every_manifest = {**others, **workspace_roots, **manifests}
        tree.workspace_links = workspace_dependency_dirs(every_manifest, wanted)
        wanted = [*wanted, *tree.workspace_links]
        lint_links = lint_config_member_dirs(lint_configs, every_manifest, wanted)
        tree.workspace_links = [*tree.workspace_links, *lint_links]
        wanted = [*wanted, *lint_links]
        tree.relay_sources = relay_source_dirs(referencing, sizes, wanted)
        wanted = [*wanted, *tree.relay_sources]
        tree.release_notes = release_note_dirs(sizes, wanted)
        wanted = [*wanted, *tree.release_notes]
        tree.sub_paths = list(wanted)
        tree.companions = companion_files(sizes, wanted)
        tree.companions = [*tree.companions, *(path for path in lifecycle_files if path not in tree.companions)]
        tree.companions = [
            *tree.companions,
            *(
                path
                for path in module_poms
                if path not in tree.companions and not any(is_under(path, sub) for sub in wanted)
            ),
        ]
        member_manifests = [
            path for path in workspace_member_manifests(workspace_roots, sizes, wanted) if path not in tree.companions
        ]
        tree.companions = [*tree.companions, *member_manifests]
        support = [
            path
            for path in check_support_files(declared, sizes)
            if path not in tree.companions and not any(is_under(path, sub) for sub in wanted)
        ]
        tree.companions = [*tree.companions, *support]
        if tree.relay_sources:
            # The sources of an added Relay src import files outside it (../../lang/front/en.json): read them
            # too, so the files they name are kept like those a test names.
            for member, read in _members(data):
                path = _relative(_clean(member.name), root) if member.is_file else ""
                if (
                    path
                    and path not in referencing
                    and member.size <= MAX_REQUIREMENTS_BYTES
                    and is_relay_source(path)
                    and any(is_under(path, sub) for sub in tree.relay_sources)
                ):
                    referencing[path] = read()
        referenced = [
            path
            for path in referenced_files(referencing, sizes, wanted)
            if path not in tree.companions and not any(is_under(path, sub) for sub in wanted)
        ]
        tree.referenced = referenced
        tree.companions = [*tree.companions, *referenced]
        named = [
            path
            for path in command_named_files(workflows, {**workspace_roots, **manifests}, sizes, wanted)
            if path not in tree.companions
        ]
        tree.named = named
        tree.companions = [*tree.companions, *named]
        install_manifests = {
            **{path: others[path] for path in tree.companions if path in others},
            **workspace_roots,
            **manifests,
            **yarnrcs,
        }
        local = [path for path in package_local_files(install_manifests, sizes, wanted) if path not in tree.companions]
        tree.local = local
        tree.companions = [*tree.companions, *local]
    kept = set(tree.companions)
    for extra in extra_files or []:
        try:
            kept.add(normalize(extra))
        except WorkspacePathError:
            continue
    total = 0
    for member, read in _members(data):
        name = _clean(member.name)
        if not name or name == "pax_global_header":
            continue
        if root:
            if name.rstrip("/") == root or not name.startswith(root + "/"):
                continue
            name = name[len(root) + 1 :]
        name = name.rstrip("/")
        if not name or ".git" in name.split("/"):
            continue
        if not member.is_file:
            if not member.is_dir:
                tree.skipped.append(f"{name} (not a regular file)")
            continue
        try:
            rel = normalize(name)
        except WorkspacePathError:
            tree.skipped.append(f"{name[:120]} (a name the workspace does not keep)")
            continue
        if wanted and rel not in kept and not any(is_under(rel, sub) for sub in wanted):
            continue
        total += member.size
        if total > max_bytes:
            raise ArchiveTooLargeError(
                f"the selected tree is over {max_bytes // (1024 * 1024)} MB: narrow it with sub_paths"
            )
        tree.files.append((rel, read()))
    return tree


#: A file outside the paths of :func:`narrow_archive` larger than this is left unread: the files the companion
#: detection reads there (manifests, lock files, workflows, configs, the scripts a check names) are smaller.
NARROW_OUTSIDE_MAX_BYTES = 8 * 1024 * 1024

#: The bytes :func:`narrow_archive` keeps outside its paths beyond the detection files (a test fixture, a script a
#: check names): past it such a file is left out and named, never a refusal, so a repository whose seed data,
#: pages or docs outside the paths pass the archive limit still narrows (#432).
NARROW_OUTSIDE_BUDGET_BYTES = 64 * 1024 * 1024

#: Extensions of the media and binary files :func:`narrow_archive` leaves unread outside its paths: no check
#: reads them, and they are what makes a repository archive heavy (a site's ``public/``).
_MEDIA_EXTENSIONS = frozenset(
    {
        ".7z", ".avi", ".avif", ".bin", ".bmp", ".dmg", ".eot", ".exe", ".flac", ".gif", ".gz", ".heic",
        ".ico", ".iso", ".jar", ".jpeg", ".jpg", ".m4a", ".m4v", ".mkv", ".mov", ".mp3", ".mp4", ".mpeg",
        ".ogg", ".otf", ".pdf", ".png", ".psd", ".rar", ".tar", ".tgz", ".tif", ".tiff", ".ttf", ".wav", ".webm",
        ".svg", ".webp", ".woff", ".woff2", ".xz", ".zip",
    }
)  # fmt: skip


@dataclass
class NarrowedArchive:
    """What :func:`narrow_archive` kept: a tar.gz of the selected members, their names unchanged."""

    data: bytes
    #: Regular files kept under the paths, and outside them.
    kept_under: int = 0
    kept_outside: int = 0
    #: The files left unread outside the paths (repository paths below the top directory), with their bytes.
    left_out: list[str] = field(default_factory=list)
    left_out_bytes: int = 0


def _under_any(rel: str, paths: list[str]) -> bool:
    return any(rel == sub or rel.startswith(sub + "/") for sub in paths)


def _detection_file(rel: str) -> bool:
    """Whether :func:`extract_tree` reads *rel* outside the sub_paths to detect and widen the checks: a manifest,
    lock file, requirements file or tool configuration (``detect.is_companion_name``), a workflow, the checks file
    and the rest of ``.xtm-one/``, the actionlint configuration. Kept within the archive limit, never budgeted."""
    name = rel.rsplit("/", 1)[-1]
    return (
        rel.startswith(".xtm-one/")
        or is_workflow(rel)
        or rel in ACTIONLINT_CONFIGS
        or is_companion_name(name)
        or is_requirements(rel)
        or is_referencing_config(rel)
    )


def _mb(size: int) -> str:
    return f"{round(size / (1024 * 1024), 1)} MB"


def narrow_archive(
    source: IO[bytes],
    paths: list[str],
    *,
    max_bytes: int,
    outside_max_bytes: int = NARROW_OUTSIDE_MAX_BYTES,
    outside_budget_bytes: int = NARROW_OUTSIDE_BUDGET_BYTES,
    max_output_bytes: int | None = None,
) -> NarrowedArchive:
    """A repository tar archive read as a stream, repacked with only what a checkout under *paths* reads.

    Kept: every regular file under *paths* (repository paths, below the archive's single top directory), and
    outside them every detection file up to *outside_max_bytes* (:func:`_detection_file`: manifests, lock files,
    workflows, ``.xtm-one/``, tool configs) plus the other small text files in archive order up to
    *outside_budget_bytes* (a fixture a test names, a script a check runs), so :func:`extract_tree` still finds
    the companions, the declared checks and the directories it widens to. Left out (named in ``left_out``): the
    media files, the large files and the files past the outside budget, never a refusal for them. Member names
    are kept as they are, the top directory included (it names the commit). Raises :class:`ArchiveTooLargeError`
    once the kept files pass *max_bytes* (the message names the bytes under the paths and outside them apart),
    once the compressed archive it writes passes *max_output_bytes* (the bytes held in memory, when given), or
    when *source* is not a tar archive.
    The archive is read once, member by member, so its size is bounded by the disk it is streamed from, never
    by memory.
    """
    wanted = [normalize(sub) for sub in paths if str(sub).strip().strip("/")]
    out = io.BytesIO()
    narrowed = NarrowedArchive(data=b"")
    under_bytes = detection_bytes = other_bytes = 0
    try:
        # Opened apart so a stream that is not a tar is refused with its reason; entered by the ``with`` below.
        reader = tarfile.open(fileobj=source, mode="r|*")  # noqa: SIM115
    except tarfile.TarError as exc:
        raise ArchiveTooLargeError(f"the archive could not be read as a tar archive ({exc})") from exc
    with reader, tarfile.open(fileobj=out, mode="w:gz", compresslevel=1) as writer:
        for info in reader:
            if not info.isfile():
                continue
            name = _clean(info.name)
            rel = name.split("/", 1)[1] if "/" in name else name
            if not rel or ".git" in rel.split("/"):
                continue
            under = _under_any(rel, wanted)
            if under:
                under_bytes += info.size
            else:
                suffix = ("." + rel.rsplit(".", 1)[1].casefold()) if "." in rel.rsplit("/", 1)[-1] else ""
                detection = _detection_file(rel)
                if (
                    info.size > outside_max_bytes
                    or (suffix in _MEDIA_EXTENSIONS and not detection)
                    or (not detection and other_bytes + info.size > outside_budget_bytes)
                ):
                    narrowed.left_out.append(rel)
                    narrowed.left_out_bytes += info.size
                    continue
                if detection:
                    detection_bytes += info.size
                else:
                    other_bytes += info.size
            if under_bytes + detection_bytes + other_bytes > max_bytes:
                raise ArchiveTooLargeError(
                    f"the files under {', '.join(wanted)} ({_mb(under_bytes)}) and the detection files outside them "
                    f"({_mb(detection_bytes)}, plus {_mb(other_bytes)} of other small files) are over "
                    f"{max_bytes // (1024 * 1024)} MB: narrow the sub_paths"
                )
            handle = reader.extractfile(info)
            writer.addfile(info, handle)
            # The compressor buffers its output, so this check lags by at most its buffer; the one after the
            # archive is closed is exact.
            if max_output_bytes is not None and out.tell() > max_output_bytes:
                raise _output_too_large(wanted, max_output_bytes, under_bytes + detection_bytes + other_bytes)
            if under:
                narrowed.kept_under += 1
            else:
                narrowed.kept_outside += 1
    if max_output_bytes is not None and out.tell() > max_output_bytes:
        raise _output_too_large(wanted, max_output_bytes, under_bytes + detection_bytes + other_bytes)
    narrowed.data = out.getvalue()
    return narrowed


def _output_too_large(wanted: list[str], max_output_bytes: int, selected: int) -> ArchiveTooLargeError:
    return ArchiveTooLargeError(
        f"the narrowed archive of {', '.join(wanted)} is over {max_output_bytes // (1024 * 1024)} MB "
        f"compressed ({_mb(selected)} of files): narrow the sub_paths"
    )
