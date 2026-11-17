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

"""``code_interpreter(packages=[...])``: wheels installed into the workspace, on the server.

The sandbox has no network and runs no installer; the platform resolves each
requested package on its package index (``CODE_INTERPRETER_PACKAGE_INDEX_URL``,
a PEP 691 / PEP 503 "simple" index: PyPI or a mirror), downloads a WHEEL
(never a source distribution: no build step, no code run anywhere), checks it
against the sha256 the index publishes, and unpacks it into
``workspace/.venv/site-packages``, which the sandbox appends to ``sys.path``.

Only distributions on ``CODE_INTERPRETER_PACKAGE_ALLOWLIST`` are installed,
the requested ones and their dependencies alike; a dependency off the list is
named as not installed (the sandbox may already provide it). Empty list:
installs are disabled. ``workspace/.venv/xtm-installed.json`` records what is
installed, so an upgrade removes the files of the version it replaces.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import time
import zipfile
from dataclasses import dataclass, field
from email.parser import HeaderParser
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin

from app.core.utils import format_exception_message
from app.services.sandbox_workspace import metrics
from app.services.sandbox_workspace.manifest import ManifestEntry, QuotaExceededError
from app.services.sandbox_workspace.paths import VENV_SITE_PACKAGES, WorkspacePathError, normalize
from app.services.sandbox_workspace.session import PendingFile, WorkspaceSession, sha256_bytes

logger = logging.getLogger(__name__)

DEFAULT_INDEX = "https://pypi.org/simple/"
INSTALLED_RECORD = ".venv/xtm-installed.json"
MAX_PACKAGES_PER_CALL = 10
MAX_RESOLVED = 30
MAX_INSTALL_BYTES = 256 * 1024 * 1024
_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json, text/html;q=0.1"


@dataclass
class InstallReport:
    installed: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    skipped_dependencies: list[str] = field(default_factory=list)
    disabled: bool = False

    def format(self) -> str:
        if self.disabled:
            return (
                "[Packages: installs are disabled on this platform (no CODE_INTERPRETER_PACKAGE_ALLOWLIST); "
                "nothing was installed. The code ran with the sandbox's own packages.]"
            )
        parts: list[str] = []
        if self.installed:
            parts.append("installed into workspace/.venv: " + ", ".join(self.installed))
        if self.already:
            parts.append("already installed: " + ", ".join(self.already))
        if self.refused:
            parts.append("not installed: " + "; ".join(self.refused))
        if self.skipped_dependencies:
            parts.append(
                "dependencies not on the allow-list, not installed (the sandbox may provide them): "
                + ", ".join(self.skipped_dependencies)
            )
        return "[Packages: " + ("; ".join(parts) or "nothing to install") + ".]"


def _canonical(name: str) -> str:
    from packaging.utils import canonicalize_name

    return str(canonicalize_name(name))


def allowed_packages() -> frozenset[str]:
    from app.core.config import settings

    return frozenset(_canonical(name) for name in settings.code_interpreter_allowed_packages)


def index_url() -> str:
    from app.core.config import settings

    url = (settings.code_interpreter_package_index_url or "").strip() or DEFAULT_INDEX
    return url if url.endswith("/") else url + "/"


def supported_tags(*, pure_only: bool) -> set:
    """The wheel tags the sandbox's interpreter takes: this process's, or pure-Python ones only."""
    from packaging.tags import sys_tags

    tags = set(sys_tags())
    if pure_only:
        tags = {tag for tag in tags if tag.abi == "none" and tag.platform == "any"}
    return tags


class _SimpleIndexPage(HTMLParser):
    """The anchors of a PEP 503 project page."""

    def __init__(self) -> None:
        super().__init__()
        self.files: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        href = values.get("href") or ""
        url, fragment = urldefrag(href)
        sha = fragment.split("=", 1)[1] if fragment.startswith("sha256=") else ""
        self.files.append(
            {
                "filename": url.rsplit("/", 1)[-1],
                "url": url,
                "hashes": {"sha256": sha} if sha else {},
                "requires-python": values.get("data-requires-python"),
                "yanked": "data-yanked" in values,
            }
        )


async def _project_files(client, name: str) -> list[dict]:
    base = index_url()
    page = urljoin(base, f"{name}/")
    response = await client.get(page, headers={"Accept": _JSON_ACCEPT})
    if response.status_code == 404:
        return []
    response.raise_for_status()
    if "json" in response.headers.get("content-type", ""):
        files = response.json().get("files", [])
    else:
        parser = _SimpleIndexPage()
        parser.feed(response.text)
        files = parser.files
    for entry in files:
        entry["url"] = urljoin(str(response.url), entry.get("url") or "")
    return files


def _choose(files: list[dict], specifier, tags: set):
    """The newest wheel matching *specifier* and *tags*: ``(version, entry)`` or ``None``."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.utils import InvalidWheelFilename, parse_wheel_filename
    from packaging.version import Version

    python = Version(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    best = None
    for entry in files:
        filename = entry.get("filename") or ""
        if not filename.endswith(".whl") or entry.get("yanked") not in (None, False):
            continue
        if not (entry.get("hashes") or {}).get("sha256"):
            continue
        try:
            _dist, version, _build, wheel_tags = parse_wheel_filename(filename)
        except InvalidWheelFilename:
            continue
        if version.is_prerelease and not specifier.prereleases:
            continue
        if specifier and not specifier.contains(version, prereleases=True):
            continue
        requires = entry.get("requires-python")
        if requires:
            try:
                fits = SpecifierSet(requires).contains(python, prereleases=True)
            except InvalidSpecifier:
                logger.debug("Skipping %s: unreadable Requires-Python %r", filename, requires)
                fits = False
            if not fits:
                continue
        if not (set(wheel_tags) & tags):
            continue
        if best is None or version > best[0]:
            best = (version, entry)
    return best


def _unpack(data: bytes) -> tuple[list[tuple[str, bytes]], list[str]]:
    """``(site-packages files, Requires-Dist lines)`` of a wheel: purelib / platlib only."""
    files: list[tuple[str, bytes]] = []
    requires: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        for info in wheel.infolist():
            if info.is_dir():
                continue
            name = info.filename
            head, _, rest = name.partition("/")
            if head.endswith(".data"):
                kind, _, inner = rest.partition("/")
                if kind not in ("purelib", "platlib") or not inner:
                    continue
                name = inner
            try:
                rel = normalize(name)
            except WorkspacePathError:
                continue
            content = wheel.read(info)
            files.append((rel, content))
            if head.endswith(".dist-info") and rest == "METADATA":
                requires = HeaderParser().parsestr(content.decode("utf-8", errors="replace")).get_all("Requires-Dist") or []
    return files, requires


async def install(session: WorkspaceSession, requirements: list[str], *, pure_only: bool) -> InstallReport:
    """Install *requirements* into the workspace's ``.venv``; never raises for a package it cannot install."""
    from packaging.requirements import InvalidRequirement, Requirement

    from app.core.egress import egress_client

    report = InstallReport()
    allowed = allowed_packages()
    if not allowed:
        report.disabled = True
        metrics.operation("install", "disabled")
        return report
    queue: list[tuple[object, bool]] = []
    for raw in requirements[:MAX_PACKAGES_PER_CALL]:
        try:
            queue.append((Requirement(str(raw)), True))
        except InvalidRequirement:
            report.refused.append(f"{str(raw)[:80]} (not a requirement: name[==version])")
    if len(requirements) > MAX_PACKAGES_PER_CALL:
        report.refused.append(f"{len(requirements) - MAX_PACKAGES_PER_CALL} more (at most {MAX_PACKAGES_PER_CALL} a call)")
    try:
        _entry, raw_record = await session.get_bytes(INSTALLED_RECORD)
        record: dict = json.loads(raw_record)
    except (FileNotFoundError, ValueError):
        record = {}
    entries = await session.entries(refresh=True)
    tags = supported_tags(pure_only=pure_only)
    writes: list[tuple[str, bytes]] = []
    removals: set[str] = set()
    total = 0
    seen: set[str] = set()
    async with egress_client(timeout=60, follow_redirects=True) as client:
        while queue and len(seen) < MAX_RESOLVED:
            requirement, requested = queue.pop(0)
            name = _canonical(requirement.name)
            if name in seen:
                continue
            seen.add(name)
            if requirement.marker is not None and not requested and not requirement.marker.evaluate({"extra": ""}):
                continue
            if name not in allowed:
                (report.refused if requested else report.skipped_dependencies).append(
                    f"{name} (not on the package allow-list)" if requested else name
                )
                continue
            held = record.get(name)
            if held and (not requirement.specifier or requirement.specifier.contains(held["version"], prereleases=True)):
                report.already.append(f"{name}=={held['version']}")
                continue
            try:
                files = await _project_files(client, name)
                chosen = _choose(files, requirement.specifier, tags)
                if chosen is None:
                    what = "a pure-Python wheel" if pure_only else "a wheel for this sandbox"
                    report.refused.append(f"{name}{requirement.specifier} (no {what} on the index)")
                    continue
                version, entry = chosen
                response = await client.get(entry["url"])
                response.raise_for_status()
                data = response.content
            except Exception as exc:
                report.refused.append(f"{name} (the index could not be read: {format_exception_message(exc)[:120]})")
                continue
            if sha256_bytes(data) != entry["hashes"]["sha256"].lower():
                report.refused.append(f"{name}=={version} (its sha256 does not match the index's)")
                continue
            try:
                unpacked, requires = _unpack(data)
            except zipfile.BadZipFile:
                report.refused.append(f"{name}=={version} (not a readable wheel)")
                continue
            size = sum(len(content) for _path, content in unpacked)
            if total + size > MAX_INSTALL_BYTES:
                report.refused.append(f"{name}=={version} (the install is over {MAX_INSTALL_BYTES // (1024 * 1024)} MB)")
                continue
            total += size
            if held:
                removals.update(f"{VENV_SITE_PACKAGES}/{path}" for path in held.get("files", []))
            new_paths = [path for path, _content in unpacked]
            removals.difference_update(f"{VENV_SITE_PACKAGES}/{path}" for path in new_paths)
            writes.extend((f"{VENV_SITE_PACKAGES}/{path}", content) for path, content in unpacked)
            record[name] = {"version": str(version), "files": new_paths}
            report.installed.append(f"{name}=={version}")
            for line in requires:
                try:
                    queue.append((Requirement(line), False))
                except InvalidRequirement:
                    continue
    if not report.installed:
        metrics.operation("install", "nothing")
        return report
    writes.append((INSTALLED_RECORD, json.dumps(record, indent=1, sort_keys=True).encode("utf-8")))
    now = time.time()
    pending = [
        PendingFile(ManifestEntry(path=path, size=len(content), sha256=sha256_bytes(content), mtime=now), data=content)
        for path, content in writes
    ]
    try:
        await session.commit(pending, sorted(p for p in removals if p in entries), operation="install")
    except QuotaExceededError as exc:
        report.refused = [f"{item} (workspace quota)" for item in report.installed] + report.refused
        report.installed = []
        report.refused.append(str(exc))
        metrics.operation("install", "quota")
        return report
    session.note_packages()
    metrics.operation("install", "ok")
    return report
