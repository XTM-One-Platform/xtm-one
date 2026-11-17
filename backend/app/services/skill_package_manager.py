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

"""Skill package import/export helpers.

Implements package parsing and ZIP generation for Agent Skills with
``versionning/`` history support.
"""

from __future__ import annotations

import io
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any

from app.core.archive_reading import ArchiveTooLargeError, read_bounded
from app.services.skill_source import frontmatter_bounds, split_skill_frontmatter

_MAX_PACKAGE_BYTES = 20 * 1024 * 1024
_MAX_FILE_BYTES = 5 * 1024 * 1024
_MAX_TOTAL_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_MAX_FILE_COUNT = 2000
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Both ``references/`` and the Anthropic-style singular ``reference/`` are
# retained — the mapping layer in agent_skills accepts either spelling.
# ``examples/`` is imported/exported as-is per the package format
# (never mapped to tools or KBs).
_RESOURCE_PREFIXES = ("scripts/", "references/", "reference/", "assets/", "examples/")
_VERSIONNING_PREFIX = "versionning/"
_MARKDOWN_SUFFIXES = (".md", ".markdown")
_ARCHIVE_METADATA_NAMES = frozenset({".DS_Store", "Thumbs.db", ".gitkeep"})
# Bulk import: a single archive may carry many skills, but not an
# unbounded number — the review screen and the per-skill import loop both stay
# usable only for archives of a sane size.
_MAX_BULK_SKILLS = 100

# Why a file inside a bulk archive was not turned into a skill. Kept as a
# closed set so the UI can group and translate the reasons instead of printing
# free-form strings.
BULK_IGNORE_REASONS = (
    "no_frontmatter",
    "unsupported_file_type",
    "not_a_skill_root",
    "archive_metadata",
    "too_large",
    "invalid_encoding",
    "ambiguous_owner",
)


@dataclass(slots=True)
class SkillPackageResource:
    path: str
    content: bytes


@dataclass(slots=True)
class SkillPackageVersion:
    number: int
    content: str
    description: str | None
    compatibility: str | None
    created_at: str | None = None


@dataclass(slots=True)
class ParsedSkillPackage:
    name: str
    description: str
    compatibility: str | None
    root_content: str
    resources: list[SkillPackageResource]
    versions: list[SkillPackageVersion]
    active_version: int
    warnings: list[str]
    #: The frontmatter ``license`` (Agent Skills spec), if any.
    license: str | None = None


@dataclass(slots=True)
class IgnoredArchiveEntry:
    """A file of a bulk archive that did not become a skill, and why."""

    path: str
    reason: str


@dataclass(slots=True)
class BulkSkillEntry:
    """One skill candidate found in a bulk archive.

    ``package`` is ``None`` when the candidate cannot be imported — ``error``
    then carries the reason, and the entry is still returned so the review
    screen can show it instead of dropping it silently.
    """

    path: str
    name: str | None
    description: str | None
    compatibility: str | None
    resource_count: int
    version_count: int
    warnings: list[str]
    error: str | None
    package: ParsedSkillPackage | None


@dataclass(slots=True)
class ParsedBulkPackage:
    skills: list[BulkSkillEntry]
    ignored: list[IgnoredArchiveEntry]


def _safe_zip_names(raw: bytes) -> list[str]:
    if len(raw) > _MAX_PACKAGE_BYTES:
        raise ValueError(
            f"Package exceeds maximum size of {_MAX_PACKAGE_BYTES // (1024 * 1024)} MB."
        )
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            infos = [i for i in zf.infolist() if not i.filename.endswith("/")]
    except zipfile.BadZipFile as err:
        raise ValueError("Invalid ZIP file.") from err
    if not infos:
        raise ValueError("ZIP package is empty.")
    if len(infos) > _MAX_FILE_COUNT:
        raise ValueError(f"Package contains too many files (max {_MAX_FILE_COUNT}).")
    # Guard against zip bombs: an early refusal on the declared decompressed sizes;
    # ``_read_zip_member`` bounds what each member really expands to.
    total_uncompressed = sum(info.file_size for info in infos)
    if total_uncompressed > _MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise ValueError(
            "Package decompressed size exceeds maximum of "
            f"{_MAX_TOTAL_UNCOMPRESSED_BYTES // (1024 * 1024)} MB."
        )
    names = [info.filename for info in infos]
    for name in names:
        # PurePosixPath treats backslashes as ordinary filename characters,
        # so a Windows-style "scripts/..\\..\\evil.py" would slip past the
        # ".." parts check below and could traverse on extraction when the
        # embedded workspace ZIP is unpacked (zip-slip). Package paths are
        # forward-slash-only per the ZIP spec — reject backslashes outright.
        if "\\" in name:
            raise ValueError(f"Unsafe ZIP path detected: {name}")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe ZIP path detected: {name}")
    return names


def _read_zip_member(zf: zipfile.ZipFile, name: str, *, error: str) -> bytes:
    """A ZIP member's bytes, ``ArchiveTooLargeError(error)`` past ``_MAX_FILE_BYTES``.

    The declared size is an early refusal only: whoever built the archive sets
    it. ``read_bounded`` decompresses at most one byte past the smaller of the
    cap and the declared size, so an under-declared member is refused as
    unreadable having decompressed no more than it declared.
    """
    if zf.getinfo(name).file_size > _MAX_FILE_BYTES:
        raise ArchiveTooLargeError(error)
    try:
        return read_bounded(zf, name, _MAX_FILE_BYTES)
    except ArchiveTooLargeError:
        raise ArchiveTooLargeError(error) from None
    except zipfile.BadZipFile as err:
        raise ValueError("Invalid ZIP file.") from err


def validate_frontmatter_warnings(
    content: str | None,
    *,
    fallback_name: str | None = None,
    fallback_description: str | None = None,
) -> list[str]:
    """Return non-blocking warnings about frontmatter package-export readiness.

    Returns an empty list when the content has valid, package-compliant
    frontmatter.  Otherwise returns human-readable warnings identifying
    each missing or malformed field.  ``fallback_name`` / ``fallback_description``
    are the skill's own properties, which the export writes into a frontmatter
    that lacks them (``complete_package_frontmatter``): a field they cover is
    judged as the export will write it.
    """
    if not content:
        return ["Skill has no content — package export requires a SKILL.md body with frontmatter."]
    meta, _ = split_skill_frontmatter(content)
    meta = {
        **meta,
        **_missing_frontmatter_fields(meta, name=fallback_name, description=fallback_description),
    }
    warnings: list[str] = []
    name = (meta.get("name") or "").strip()
    description = (meta.get("description") or "").strip()
    if not name:
        warnings.append("SKILL.md frontmatter is missing required `name`.")
    elif len(name) > 64:
        warnings.append("`name` exceeds max length (64).")
    elif not _NAME_RE.match(name):
        warnings.append(
            "`name` must contain lowercase letters, numbers, and hyphens only; "
            "no leading/trailing/consecutive hyphens."
        )
    if not description:
        warnings.append("SKILL.md frontmatter is missing required `description`.")
    elif len(description) > 1024:
        warnings.append("`description` exceeds max length (1024).")
    compatibility = (meta.get("compatibility") or "").strip()
    if compatibility and len(compatibility) > 500:
        warnings.append("`compatibility` exceeds max length (500).")
    return warnings


def _validate_metadata(meta: dict[str, str], *, parent_dir_name: str | None = None) -> None:
    name = (meta.get("name") or "").strip()
    description = (meta.get("description") or "").strip()
    compatibility = (meta.get("compatibility") or "").strip()
    if not name:
        raise ValueError("SKILL.md frontmatter is missing required `name`.")
    if len(name) > 64:
        raise ValueError("`name` exceeds max length (64).")
    if not _NAME_RE.match(name):
        raise ValueError(
            "`name` must contain lowercase letters, numbers, and hyphens only; no leading/trailing/consecutive hyphens."
        )
    if parent_dir_name and name != parent_dir_name:
        raise ValueError(
            f"`name` ({name}) must match parent directory name ({parent_dir_name})."
        )
    if not description:
        raise ValueError("SKILL.md frontmatter is missing required `description`.")
    if len(description) > 1024:
        raise ValueError("`description` exceeds max length (1024).")
    if compatibility and len(compatibility) > 500:
        raise ValueError("`compatibility` exceeds max length (500).")


def _skill_root_prefix(names: list[str]) -> tuple[str, str]:
    candidates: list[str] = []
    for name in names:
        path = PurePosixPath(name)
        if path.name != "SKILL.md":
            continue
        # Root SKILL.md is either "SKILL.md" or "<skill-name>/SKILL.md".
        # Ignore historical files in versionning/*/SKILL.md.
        if len(path.parts) in (1, 2):
            candidates.append(name)
    if len(candidates) != 1:
        raise ValueError(
            "Package must contain exactly one root SKILL.md file (found "
            f"{len(candidates)})."
        )
    skill_md = candidates[0]
    if skill_md == "SKILL.md":
        return "", skill_md
    return skill_md[: -len("SKILL.md")], skill_md


def parse_skill_package_zip(raw: bytes) -> ParsedSkillPackage:
    names = _safe_zip_names(raw)
    prefix, skill_md_path = _skill_root_prefix(names)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return _parse_skill_at(zf, names, prefix=prefix, skill_md_path=skill_md_path)


def _parse_skill_at(
    zf: zipfile.ZipFile,
    names: list[str],
    *,
    prefix: str,
    skill_md_path: str,
    strict_parent_name: bool = True,
    include_resources: bool = True,
) -> ParsedSkillPackage:
    """Parse the single skill rooted at *prefix* out of an open archive.

    Split out of :func:`parse_skill_package_zip` so the bulk importer
    can run it once per skill found in a multi-skill archive.

    ``skill_md_path`` is the archive path of the skill document: ``SKILL.md``
    for spec-compliant packages, ``<skill-name>.md`` for the looser layouts a
    bulk archive may carry.  ``strict_parent_name`` keeps the spec's
    "frontmatter ``name`` must equal the parent directory" rule a hard error
    for single-package imports; the bulk path downgrades it to a warning so one
    mismatched folder cannot sink a whole archive.  ``include_resources``
    disables resource/``versionning`` collection when a directory holds several
    skill documents and no resource can be attributed to one of them.
    """
    parent_name = None
    if prefix:
        parent = PurePosixPath(prefix.rstrip("/")).name
        parent_name = parent or None
    root_rel = skill_md_path[len(prefix) :] if prefix else skill_md_path
    label = "Root SKILL.md" if root_rel == "SKILL.md" else f"Root skill file {root_rel}"

    warnings: list[str] = []
    root_bytes = _read_zip_member(
        zf, skill_md_path, error=f"{label} exceeds maximum file size."
    )
    try:
        root_content = root_bytes.decode("utf-8")
    except UnicodeDecodeError as err:
        raise ValueError(f"{label} must be UTF-8.") from err
    meta, _ = split_skill_frontmatter(root_content)
    if strict_parent_name:
        _validate_metadata(meta, parent_dir_name=parent_name)
    else:
        _validate_metadata(meta)
        declared = (meta.get("name") or "").strip()
        if parent_name and declared != parent_name:
            warnings.append(
                f"`name` ({declared}) does not match parent directory name "
                f"({parent_name}); the frontmatter name is used."
            )
    if root_content.count("\n") + 1 > 500:
        warnings.append(f"{label} is longer than 500 lines.")

    resources: list[SkillPackageResource] = []
    for name in sorted(names) if include_resources else []:
        if prefix and not name.startswith(prefix):
            continue
        rel = name[len(prefix) :] if prefix else name
        if rel == root_rel:
            continue
        if not rel.startswith(_RESOURCE_PREFIXES):
            continue
        payload = _read_zip_member(
            zf, name, error=f"Resource file exceeds maximum file size: {rel}"
        )
        resources.append(SkillPackageResource(path=rel, content=payload))

    manifest_rel = "versionning/manifest.json"
    manifest_path = f"{prefix}{manifest_rel}" if prefix else manifest_rel
    versions: list[SkillPackageVersion] = []
    active_version: int | None = None
    if include_resources and manifest_path in names:
        manifest_bytes = _read_zip_member(
            zf,
            manifest_path,
            error="versionning/manifest.json exceeds maximum file size.",
        )
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except Exception as err:
            raise ValueError("Invalid versionning/manifest.json.") from err
        if manifest.get("schema_version") != 1:
            raise ValueError("Unsupported versionning manifest schema_version.")
        raw_versions = manifest.get("versions")
        if not isinstance(raw_versions, list) or not raw_versions:
            raise ValueError("versionning manifest must include non-empty `versions`.")
        prev_num = 0
        for row in raw_versions:
            if not isinstance(row, dict):
                raise ValueError("Invalid versionning manifest entry.")
            num = row.get("number")
            rel_path = row.get("path")
            if not isinstance(num, int) or num <= 0:
                raise ValueError("Version entry `number` must be a positive integer.")
            if num <= prev_num:
                raise ValueError("Version numbers must be strictly increasing.")
            prev_num = num
            if not isinstance(rel_path, str) or not rel_path.endswith("SKILL.md"):
                raise ValueError("Version entry `path` must target a SKILL.md file.")
            full_path = f"{prefix}versionning/{rel_path}" if prefix else f"versionning/{rel_path}"
            if full_path not in names:
                raise ValueError(f"Missing version file: versionning/{rel_path}")
            data = _read_zip_member(
                zf,
                full_path,
                error=f"Version file exceeds maximum size: {rel_path}",
            )
            try:
                version_content = data.decode("utf-8")
            except UnicodeDecodeError as err:
                raise ValueError(f"Version file must be UTF-8: {rel_path}") from err
            version_meta, _ = split_skill_frontmatter(version_content)
            # Historical versions may predate a skill rename, so the
            # parent-directory name match is only enforced on the root
            # SKILL.md — version snapshots are validated standalone.
            _validate_metadata(version_meta)
            versions.append(
                SkillPackageVersion(
                    number=num,
                    content=version_content,
                    description=version_meta.get("description"),
                    compatibility=version_meta.get("compatibility"),
                    created_at=row.get("created_at")
                    if isinstance(row.get("created_at"), str)
                    else None,
                )
            )

        active_version = manifest.get("active_version")
        if not isinstance(active_version, int):
            raise ValueError("versionning manifest `active_version` must be an integer.")
        active_row = next((v for v in versions if v.number == active_version), None)
        if active_row is None:
            raise ValueError("versionning manifest `active_version` not found in versions.")
        # The platform always treats the highest version number as active
        # (rollbacks create a new head version), so a manifest whose
        # active_version is not the latest would import with the wrong
        # version shown as active.
        if active_version != versions[-1].number:
            raise ValueError(
                "versionning manifest `active_version` must be the latest "
                "version number — export rollbacks as a new head version."
            )
        if active_row.content != root_content:
            raise ValueError(
                "Root SKILL.md must match content of versionning active_version."
            )

    if not versions:
        versions = [
            SkillPackageVersion(
                number=1,
                content=root_content,
                description=meta.get("description"),
                compatibility=meta.get("compatibility"),
            )
        ]
        active_version = 1
        warnings.append("No versionning manifest found; initialized with root SKILL.md only.")

    if active_version is None:
        raise ValueError("Unable to resolve versionning manifest `active_version`.")

    return ParsedSkillPackage(
        name=meta["name"],
        description=meta["description"],
        compatibility=meta.get("compatibility"),
        license=meta.get("license"),
        root_content=root_content,
        resources=resources,
        versions=versions,
        active_version=active_version,
        warnings=warnings,
    )


def _is_archive_metadata(name: str) -> bool:
    """True for OS/archiver bookkeeping files that carry no skill content."""
    path = PurePosixPath(name)
    if "__MACOSX" in path.parts:
        return True
    base = path.name
    return base in _ARCHIVE_METADATA_NAMES or base.startswith("._")


def _probe_skill_markdown(
    zf: zipfile.ZipFile, name: str
) -> tuple[dict[str, str] | None, str | None]:
    """Return ``(frontmatter, ignore_reason)`` for a candidate skill document.

    A markdown file only counts as a skill when it carries YAML frontmatter
    with both ``name`` and ``description``; anything else (a README, a design
    note, a binary blob renamed ``.md``) is reported as ignored rather than as
    an archive-level error.
    """
    try:
        payload = _read_zip_member(zf, name, error="too_large")
    except ArchiveTooLargeError:
        return None, "too_large"
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None, "invalid_encoding"
    meta, _ = split_skill_frontmatter(content)
    if not (meta.get("name") or "").strip() or not (meta.get("description") or "").strip():
        return None, "no_frontmatter"
    return meta, None


def _skill_dir_of(path: str) -> str:
    """Directory prefix holding *path* (``""`` for an archive-root file)."""
    head, sep, _ = path.rpartition("/")
    return f"{head}/" if sep else ""


def _payload_owner(path: str, roots: set[str]) -> str | None:
    """Return the accepted skill root that owns *path* as one of its files.

    Ownership only flows through the mapped folders — ``scripts/``,
    ``references/``, ``reference/``, ``assets/``, ``examples/`` and
    ``versionning/``. That is what keeps a ``versionning/0001/SKILL.md``
    snapshot (valid frontmatter, real skill content) from being mistaken for a
    skill of its own, while a sibling directory of a skill folder stays free to
    hold another skill. The deepest matching root wins, so nested layouts
    resolve to the closest owner.
    """
    for root in sorted(roots, key=len, reverse=True):
        if not path.startswith(root):
            continue
        rel = path[len(root) :]
        if rel.startswith(_RESOURCE_PREFIXES) or rel.startswith(_VERSIONNING_PREFIX):
            return root
    return None


def _discover_skill_roots(
    probes: dict[str, tuple[dict[str, str] | None, str | None]],
) -> dict[str, list[str]]:
    """Group skill documents by the directory that holds them.

    Walking shallowest-first matters: a root must be accepted before the files
    below it are tested for ownership, otherwise a version snapshot would be
    read as a skill. Directories are discovered at **any** depth, so an archive
    zipped from a parent folder (``skills/<skill-name>/SKILL.md``) and one
    zipped from its contents (``<skill-name>/SKILL.md``) both work, and a
    ``README.md`` sitting next to the skill folders changes nothing.
    """
    roots: dict[str, list[str]] = {}
    for path in sorted(probes, key=lambda name: (name.count("/"), name)):
        meta, _reason = probes[path]
        if meta is None:
            continue
        if _payload_owner(path, set(roots)) is not None:
            continue
        roots.setdefault(_skill_dir_of(path), []).append(path)
    return roots


def _select_skill_documents(
    roots: dict[str, list[str]],
    ignored: list[IgnoredArchiveEntry],
) -> dict[str, list[str]]:
    """Keep one skill document per root when the spec's ``SKILL.md`` is present.

    A package that ships both ``SKILL.md`` and a copy named after the skill
    must not import the same skill twice; anything else in the folder that
    happens to carry frontmatter is reported rather than dropped silently.
    """
    selected: dict[str, list[str]] = {}
    for root, documents in roots.items():
        spec_files = [path for path in documents if path[len(root) :] == "SKILL.md"]
        if spec_files:
            selected[root] = spec_files
            for path in documents:
                if path not in spec_files:
                    ignored.append(IgnoredArchiveEntry(path=path, reason="not_a_skill_root"))
        else:
            selected[root] = documents
    return selected


def parse_skill_bulk_package_zip(raw: bytes) -> ParsedBulkPackage:
    """Parse a ZIP holding **several** skills into per-skill import candidates.

    A skill is a markdown file carrying YAML frontmatter with ``name`` and
    ``description``, and the directory holding it is that skill's root. Roots
    are found at any depth, so all of these work in one archive:

    - ``<skill-name>/SKILL.md`` with its ``scripts/`` / ``references/`` /
      ``assets/`` / ``examples/`` / ``versionning/`` folders — the
      `Agent Skills <https://agentskills.io>`_ layout, now allowed many times
      over instead of exactly once.
    - ``<skill-name>/<anything>.md`` — folders that name the file after the
      skill rather than using ``SKILL.md``.
    - ``<skill-name>.md`` at the archive root — a flat folder of skill files.
    - any of the above under one or more wrapper directories, which is what
      zipping a ``skills/`` folder produces.

    Every file that is neither a skill document nor one of its mapped
    resources is reported in :attr:`ParsedBulkPackage.ignored` with a
    machine-readable reason, so the caller can show a "found vs ignored"
    review before anything is written. Only archive-level problems (unreadable
    ZIP, size caps, unsafe paths, no skill at all) raise :class:`ValueError`; a
    single malformed skill is reported as a per-entry error and leaves the rest
    of the archive importable.
    """
    names = _safe_zip_names(raw)

    ignored: list[IgnoredArchiveEntry] = []
    kept: list[str] = []
    for name in names:
        if _is_archive_metadata(name):
            ignored.append(IgnoredArchiveEntry(path=name, reason="archive_metadata"))
        else:
            kept.append(name)

    entries: list[BulkSkillEntry] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        probes = {
            name: _probe_skill_markdown(zf, name)
            for name in sorted(kept)
            if name.lower().endswith(_MARKDOWN_SUFFIXES)
        }
        documents = _select_skill_documents(_discover_skill_roots(probes), ignored)
        # Resources and version history can only be attributed when a root
        # holds exactly one skill document — otherwise ownership is ambiguous
        # and the files are reported instead of being silently duplicated.
        exclusive = {root: len(paths) == 1 for root, paths in documents.items()}
        selected = {path for paths in documents.values() for path in paths}
        reported = {entry.path for entry in ignored}

        for name in sorted(kept):
            if name in selected or name in reported:
                continue
            owner = _payload_owner(name, set(documents))
            if owner is not None:
                if not exclusive[owner]:
                    ignored.append(IgnoredArchiveEntry(path=name, reason="ambiguous_owner"))
                continue
            probe = probes.get(name)
            if probe is None:
                # Not markdown at all, and no skill claims it.
                ignored.append(IgnoredArchiveEntry(path=name, reason="unsupported_file_type"))
            else:
                _meta, reason = probe
                ignored.append(
                    IgnoredArchiveEntry(path=name, reason=reason or "not_a_skill_root")
                )

        for root, paths in documents.items():
            for path in paths:
                meta = probes[path][0] or {}
                try:
                    package = _parse_skill_at(
                        zf,
                        kept,
                        prefix=root,
                        skill_md_path=path,
                        strict_parent_name=False,
                        include_resources=exclusive[root],
                    )
                except ValueError as err:
                    entries.append(
                        BulkSkillEntry(
                            path=path,
                            name=(meta.get("name") or "").strip() or None,
                            description=(meta.get("description") or "").strip() or None,
                            compatibility=None,
                            resource_count=0,
                            version_count=0,
                            warnings=[],
                            error=str(err),
                            package=None,
                        )
                    )
                    continue
                warnings = list(package.warnings)
                if not exclusive[root]:
                    warnings.append(
                        "Several skill files share this folder — resources and version "
                        "history were not attached to this skill."
                    )
                entries.append(
                    BulkSkillEntry(
                        path=path,
                        name=package.name,
                        description=package.description,
                        compatibility=package.compatibility,
                        resource_count=len(package.resources),
                        version_count=len(package.versions),
                        warnings=warnings,
                        error=None,
                        package=package,
                    )
                )

    if len(entries) > _MAX_BULK_SKILLS:
        raise ValueError(f"Archive contains too many skills (max {_MAX_BULK_SKILLS}).")
    if not entries:
        raise ValueError(
            "No skill was found in the archive — a skill is a markdown file with "
            "YAML frontmatter declaring `name` and `description`."
        )

    entries.sort(key=lambda entry: entry.path)
    # Two skills declaring the same name would silently overwrite each other on
    # import; the first one by path wins and the rest are surfaced.
    seen: set[str] = set()
    for entry in entries:
        if entry.error or not entry.name:
            continue
        if entry.name in seen:
            entry.error = (
                f"Duplicate skill name in archive: another entry already declares "
                f"`{entry.name}`."
            )
            entry.package = None
            continue
        seen.add(entry.name)

    ignored.sort(key=lambda item: item.path)
    return ParsedBulkPackage(skills=entries, ignored=ignored)


def parse_skill_markdown(raw: bytes) -> ParsedSkillPackage:
    """Parse a single SKILL.md payload as an importable package."""
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError("Root SKILL.md exceeds maximum file size.")
    try:
        root_content = raw.decode("utf-8")
    except UnicodeDecodeError as err:
        raise ValueError("Root SKILL.md must be UTF-8.") from err

    meta, _ = split_skill_frontmatter(root_content)
    _validate_metadata(meta)
    warnings = ["No versionning manifest found; initialized with root SKILL.md only."]

    return ParsedSkillPackage(
        name=meta["name"],
        description=meta["description"],
        compatibility=meta.get("compatibility"),
        license=meta.get("license"),
        root_content=root_content,
        resources=[],
        versions=[
            SkillPackageVersion(
                number=1,
                content=root_content,
                description=meta.get("description"),
                compatibility=meta.get("compatibility"),
            )
        ],
        active_version=1,
        warnings=warnings,
    )


def rewrite_frontmatter_name(content: str, new_name: str) -> str:
    """Replace the frontmatter ``name`` value in a SKILL.md document.

    Used when an import renames the skill (``as_new`` collisions): exports
    derive the package root directory from the frontmatter name, so a stale
    value would make the exported package collide with the ORIGINAL skill
    instead of the renamed one. Returns the content unchanged when there is
    no frontmatter or the ``name`` uses a block scalar (cannot be rewritten
    line-wise safely).
    """
    bounds = frontmatter_bounds(content)
    if bounds is None:
        return content
    fm_start, close_start, _ = bounds
    head = content[:fm_start]
    block = content[fm_start : close_start - 1]
    tail = content[close_start - 1 :]
    lines = block.split("\n")
    for i, line in enumerate(lines):
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "name":
            if value.strip() in (">", ">-", "|", "|-"):
                return content
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = f"{indent}name: {new_name}"
            return head + "\n".join(lines) + tail
    return content


def package_skill_name(display_name: str | None) -> str | None:
    """Return the package-compliant ``name`` for a skill's display name.

    The Agent Skills spec only allows lowercase letters, digits and single
    hyphens, 64 characters at most. ``None`` when nothing usable is left.
    """
    text = unicodedata.normalize("NFKD", display_name or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64].strip("-")
    return slug or None


# Plain scalars a YAML reader turns into a bool or null (YAML 1.1 spellings).
_YAML_NON_STRING_WORDS = frozenset({"true", "false", "yes", "no", "on", "off", "y", "n", "null"})


def _is_yaml_number(value: str) -> bool:
    try:
        float(value.replace("_", ""))
    except ValueError:
        return False
    return True


def _frontmatter_scalar_line(key: str, value: str, newline: str) -> str:
    """One ``key: value`` frontmatter entry; a risky value becomes a folded block.

    A value a YAML reader would not read back as the same string (a bool, a
    null, a number, a ``key: value`` or comment lookalike) is written as a
    folded block, which always reads as a string.
    """
    plain = (
        value[0].isalnum()
        and ": " not in value
        and " #" not in value
        and not value.endswith((":", '"', "'"))
        and value.lower() not in _YAML_NON_STRING_WORDS
        and not _is_yaml_number(value)
    )
    if plain:
        return f"{key}: {value}"
    return f"{key}: >-{newline}  {value}"


def _missing_frontmatter_fields(
    meta: dict[str, str], *, name: str | None, description: str | None
) -> dict[str, str]:
    """The ``name`` / ``description`` the export writes for a frontmatter lacking them."""
    added: dict[str, str] = {}
    if not (meta.get("name") or "").strip():
        slug = package_skill_name(name)
        if slug:
            added["name"] = slug
    if not (meta.get("description") or "").strip():
        text = " ".join((description or "").split())
        if text:
            added["description"] = text
    return added


def complete_package_frontmatter(
    content: str, *, name: str | None, description: str | None
) -> str:
    """Add the ``name`` / ``description`` a SKILL.md frontmatter lacks.

    A skill pasted or uploaded as a bare body keeps its name and description
    in the skill's own properties, while a package needs them in the SKILL.md
    frontmatter: the export fills the gap from those properties instead of
    waiting for the author to repeat them in the content. A value the
    frontmatter already carries is never touched, and *name* is reduced to the
    package format (``package_skill_name``).
    """
    meta, _ = split_skill_frontmatter(content)
    newline = "\r\n" if "\r\n" in content else "\n"
    added = _missing_frontmatter_fields(meta, name=name, description=description)
    if not added:
        return content
    lines = [_frontmatter_scalar_line(key, value, newline) for key, value in added.items()]

    bounds = frontmatter_bounds(content)
    if bounds is None:
        return f"---{newline}{newline.join(lines)}{newline}---{newline}{newline}{content}"
    fm_start, close_start, _ = bounds
    # A top-level empty ``name:`` / ``description:`` entry is dropped so the key
    # is not declared twice; an indented line belongs to another key's block.
    kept = [
        line
        for line in content[fm_start:close_start].splitlines(keepends=True)
        if not (
            line[:1] not in (" ", "\t")
            and line.partition(":")[1]
            and line.partition(":")[0].strip().lower() in added
            and not line.partition(":")[2].strip()
        )
    ]
    return (
        content[:fm_start]
        + newline.join(lines)
        + newline
        + "".join(kept)
        + content[close_start:]
    )


def derive_package_root_name(root_content: str | None, *, fallback: str) -> str:
    """Return the package root directory name for an export.

    Import validation requires the frontmatter ``name`` to match the root
    directory, so exports prefer the frontmatter name from the skill content
    and only fall back to *fallback* (the display name) when the frontmatter
    name is missing or non-conforming.
    """
    meta, _ = split_skill_frontmatter(root_content or "")
    name = (meta.get("name") or "").strip()
    if name and len(name) <= 64 and _NAME_RE.match(name):
        return name
    return fallback


def parse_skill_import_upload(raw: bytes, *, filename: str | None = None) -> ParsedSkillPackage:
    """Parse an uploaded skill import payload (ZIP package or plain SKILL.md)."""
    lower_name = (filename or "").strip().lower()
    if lower_name.endswith(".zip"):
        return parse_skill_package_zip(raw)
    if lower_name.endswith((".md", ".markdown", ".txt")):
        return parse_skill_markdown(raw)
    if raw.startswith(b"PK"):
        return parse_skill_package_zip(raw)
    return parse_skill_markdown(raw)


def _writestr_deterministic(
    zf: zipfile.ZipFile,
    arcname: str,
    payload: bytes,
) -> None:
    info = zipfile.ZipInfo(filename=arcname, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, payload)


def build_skill_package_zip(
    *,
    root_dir_name: str,
    root_skill_content: str,
    resources: list[SkillPackageResource],
    versions: list[SkillPackageVersion],
    include_versionning: bool = True,
) -> bytes:
    root = root_dir_name.strip("/")
    if not root:
        raise ValueError("Package root directory name is required.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _writestr_deterministic(
            zf, f"{root}/SKILL.md", root_skill_content.encode("utf-8")
        )

        for res in sorted(resources, key=lambda r: r.path):
            arcname = f"{root}/{res.path}"
            _writestr_deterministic(zf, arcname, res.content)

        if include_versionning:
            sorted_versions = sorted(versions, key=lambda v: v.number)
            if not sorted_versions:
                sorted_versions = [
                    SkillPackageVersion(
                        number=1,
                        content=root_skill_content,
                        description=None,
                        compatibility=None,
                    )
                ]
            manifest_versions: list[dict[str, Any]] = []
            for version in sorted_versions:
                rel = f"{version.number:04d}/SKILL.md"
                _writestr_deterministic(
                    zf,
                    f"{root}/versionning/{rel}",
                    version.content.encode("utf-8"),
                )
                entry: dict[str, Any] = {"number": version.number, "path": rel}
                if version.created_at:
                    entry["created_at"] = version.created_at
                manifest_versions.append(entry)
            active_version = sorted_versions[-1].number
            manifest = {
                "schema_version": 1,
                "active_version": active_version,
                "versions": manifest_versions,
            }
            _writestr_deterministic(
                zf,
                f"{root}/versionning/manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
    return buf.getvalue()


def sha256_hex(data: bytes) -> str:
    return sha256(data).hexdigest()
