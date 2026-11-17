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

"""Moving dashboards and widgets between XTM One, OpenCTI and OpenAEV.

Pure conversions, no I/O: the routes and the agent tools read the files and
call the platforms; these modules only turn one format into another.

* :mod:`.pack_format` - the XTM One widget pack file;
* :mod:`.opencti` - OpenCTI dashboard and widget exports (``openCTI_version``,
  a base64 manifest) to widgets, and widgets back to an OpenCTI export;
* :mod:`.openaev` - OpenAEV dashboard exports (a ZIP holding a JSON:API
  document) to widgets, and widgets back to an OpenAEV export;
* :mod:`.layout` - OpenCTI's and OpenAEV's 12-column grid to the dashboard's
  widths, and back;
* :func:`parse_any` - a file of any of these formats, recognised by content.

Every file read here is hostile until checked: size, archive entries, base64,
JSON depth, and every widget through the same checks as a widget an agent
writes (``custom_dashboard_spec``, ``dashboard_platform``). A conversion is
lossy - the dashboard has three widths and no heights, at most
``MAX_WIDGETS`` widgets; a platform type may not exist on the other side - so
each one returns a :class:`ConversionReport` saying what changed or was left
out, which the routes and tools show as is.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field

#: An uploaded file (a pack, an export) may not exceed this.
MAX_FILE_BYTES = 5 * 1024 * 1024
#: A decoded manifest or JSON document may not exceed this.
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
#: A ZIP may not hold more entries than this.
MAX_ARCHIVE_ENTRIES = 16
#: A pack holds at most this many widgets.
MAX_PACK_WIDGETS = 64
#: Notes kept in a report.
MAX_NOTES = 50


class InteropError(ValueError):
    """A file that cannot be read as a dashboard, a widget or a pack, with a readable reason."""


@dataclass(slots=True)
class ConversionReport:
    """What a conversion changed or left out, one sentence each."""

    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        if len(self.notes) < MAX_NOTES:
            self.notes.append(message)
        elif len(self.notes) == MAX_NOTES:
            self.notes.append("… and more changes not listed.")


@dataclass(slots=True)
class ParsedWidgets:
    """Widgets read from a file, ready for a pack (platform widgets carry no integration)."""

    #: What the file was: ``xtm-one-pack``, ``opencti-dashboard``, ``opencti-custom-view``, ``opencti-widget``,
    #: ``openaev-dashboard``.
    origin: str
    name: str
    description: str | None
    widgets: list[dict]
    report: ConversionReport
    #: The platform version the file names, when it names one.
    platform_version: str | None = None
    #: The inputs the widgets read (``$token$`` in their filters), checked as a definition's, unbound: an
    #: OpenCTI custom view's "current entity".
    inputs: list[dict] = field(default_factory=list)


def loads_bounded(data: bytes | str, *, what: str, limit: int = MAX_DOCUMENT_BYTES) -> object:
    """Parse JSON of at most *limit* bytes; a readable error otherwise."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > limit:
        raise InteropError(f"The {what} is larger than {limit // (1024 * 1024)} MB.")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise InteropError(f"The {what} is not valid JSON.") from None


def read_zip_entries(data: bytes, *, limit: int = MAX_DOCUMENT_BYTES) -> dict[str, bytes]:
    """The JSON entries of a ZIP, each read within *limit* bytes.

    The bound is on bytes actually read (``limit + 1`` decides), never on the
    sizes the archive declares, which an attacker writes; the total is bounded
    too, and entries other than top-level ``.json`` files are ignored.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise InteropError("The file is not a readable ZIP archive.") from None
    entries: dict[str, bytes] = {}
    total = 0
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise InteropError(f"The archive holds more than {MAX_ARCHIVE_ENTRIES} entries.")
        for info in infos:
            name = info.filename
            if info.is_dir() or "/" in name or "\\" in name or not name.endswith(".json"):
                continue
            with archive.open(info) as handle:
                chunk = handle.read(limit + 1)
            if len(chunk) > limit:
                raise InteropError(f"The archive entry {name!r:.60} expands beyond {limit // (1024 * 1024)} MB.")
            total += len(chunk)
            if total > MAX_DOCUMENT_BYTES * 2:
                raise InteropError("The archive expands beyond the size an import accepts.")
            entries[name] = chunk
    return entries


def parse_any(data: bytes, *, filename: str = "") -> ParsedWidgets:
    """Read a pack, an OpenCTI export (dashboard or widget) or an OpenAEV dashboard export."""
    from . import openaev, opencti, pack_format

    if len(data) > MAX_FILE_BYTES:
        raise InteropError(f"The file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    if data[:4] == b"PK\x03\x04":
        entries = read_zip_entries(data)
        if pack_format.PACK_ENTRY in entries:
            return pack_format.parse_pack(entries[pack_format.PACK_ENTRY])
        return openaev.parse_export_entries(entries)
    document = loads_bounded(data, what=f"file {filename!r:.60}" if filename else "file")
    if isinstance(document, dict) and document.get("format") == pack_format.PACK_FORMAT:
        return pack_format.parse_pack_document(document)
    if isinstance(document, dict) and "openCTI_version" in document:
        return opencti.parse_export_document(document)
    raise InteropError(
        "The file is not a widget pack, an OpenCTI dashboard or widget export, or an OpenAEV dashboard export (ZIP)."
    )
