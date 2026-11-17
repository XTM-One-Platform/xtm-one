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

"""What decoding finds wrong with a file a public chat widget visitor attached.

An image is decoded as the format its name says, an Office file checked to be
an OOXML package whose parts and expanded size are bounded. Decoding a crafted
file costs time and memory, so this runs in the widget document reader's
worker process (``widget_document_reader.inspect_file``), under its timeout
and address-space cap, never on a thread of the platform process. Standard
library and Pillow only: the spawned worker imports nothing else.
"""

from __future__ import annotations

import io
import struct
import zipfile

#: Five times the chat upload limit (``file_storage.MAX_UPLOAD_SIZE_BYTES``, pinned by the tests).
MAX_EXPANDED_BYTES = 200 * 1024 * 1024
#: What an Office package may declare before ``zipfile`` indexes it: its entry
#: count, and the size of its central directory, which ``zipfile`` reads whole
#: and turns into one object per entry whatever the count says.
MAX_ARCHIVE_ENTRIES = 5000
MAX_CENTRAL_DIRECTORY_BYTES = 4 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
#: What an image may declare before anything decodes its pixels: a small file
#: can declare a raster that fills memory once the model's copy is prepared.
MAX_IMAGE_PIXELS = 50_000_000
MAX_IMAGE_SIDE = 20_000

OOXML_MAIN_PARTS = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}
IMAGE_FORMATS = {"gif": "GIF", "jpeg": "JPEG", "jpg": "JPEG", "png": "PNG", "webp": "WEBP"}

_END_OF_DIRECTORY = b"PK\x05\x06"
_END_OF_DIRECTORY_BYTES = 22
_MAX_ARCHIVE_COMMENT = 0xFFFF
# ZIP64: a locator right before the record above sends ``zipfile`` to a ZIP64
# record whose count and directory size replace the record's own.
_ZIP64_LOCATOR = b"PK\x06\x07"
_ZIP64_LOCATOR_FORMAT = "<4sLQL"
_ZIP64_LOCATOR_BYTES = struct.calcsize(_ZIP64_LOCATOR_FORMAT)
_ZIP64_RECORD = b"PK\x06\x06"
_ZIP64_RECORD_FORMAT = "<4sQ2H2L4Q"
_ZIP64_RECORD_BYTES = struct.calcsize(_ZIP64_RECORD_FORMAT)


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def needs_inspection(filename: str) -> bool:
    """Whether the file is of a kind decoding checks: an image or an Office package."""
    ext = _extension(filename)
    return ext in IMAGE_FORMATS or ext in OOXML_MAIN_PARTS


def _image_problem(ext: str, data: bytes) -> str | None:
    from PIL import Image

    expected = IMAGE_FORMATS[ext]
    too_large = "The image is too large to be read: at most {side:,} pixels a side and {pixels:,} in all."
    try:
        with Image.open(io.BytesIO(data)) as image:
            found = image.format
            width, height = image.size
            # Read off the header, before any pixel is decoded.
            if max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
                return too_large.format(side=MAX_IMAGE_SIDE, pixels=MAX_IMAGE_PIXELS)
            image.verify()
    except Image.DecompressionBombError:
        return too_large.format(side=MAX_IMAGE_SIDE, pixels=MAX_IMAGE_PIXELS)
    except Exception:
        return f"The file is not a valid {ext.upper()} image."
    if found != expected:
        return f"The file is a {found} image, not a {ext.upper()} image: rename it or convert it."
    return None


def _declared_directory(data: bytes) -> tuple[int, int] | None:
    """The entry count and central-directory size ``zipfile`` can take from the package, the largest of each.

    The end-of-central-directory record found the way ``zipfile`` finds it
    (the last one in the tail an archive comment may take). Behind a ZIP64
    locator ``zipfile`` takes both from the ZIP64 record instead - at the
    locator's offset, else right before the locator - so every such record
    counts. ``None`` for a package ``zipfile`` would not open.
    """
    at = len(data) - _END_OF_DIRECTORY_BYTES
    if at < 0 or not (data.startswith(_END_OF_DIRECTORY, at) and data.endswith(b"\x00\x00")):
        at = data.rfind(_END_OF_DIRECTORY, max(0, len(data) - _END_OF_DIRECTORY_BYTES - _MAX_ARCHIVE_COMMENT))
        if at < 0 or len(data) - at < _END_OF_DIRECTORY_BYTES:
            return None
    entries, directory_bytes = struct.unpack_from("<HI", data, at + 10)
    locator_at = at - _ZIP64_LOCATOR_BYTES
    if locator_at < 0 or not data.startswith(_ZIP64_LOCATOR, locator_at):
        return entries, directory_bytes
    record_at = struct.unpack_from(_ZIP64_LOCATOR_FORMAT, data, locator_at)[2]
    records = [
        struct.unpack_from(_ZIP64_RECORD_FORMAT, data, candidate)
        for candidate in {record_at, locator_at - _ZIP64_RECORD_BYTES}
        if 0 <= candidate <= len(data) - _ZIP64_RECORD_BYTES and data.startswith(_ZIP64_RECORD, candidate)
    ]
    if not records:
        return None
    # (signature, size, versions x2, disks x2, entries on this disk, entries, directory size, offset)
    return (
        max([entries, *(max(record[6], record[7]) for record in records)]),
        max([directory_bytes, *(record[8] for record in records)]),
    )


def _directory_problem(kind: str, data: bytes) -> str | None:
    """What the package declares its central directory holds, checked before anything indexes it."""
    declared = _declared_directory(data)
    if declared is None:
        return f"The file is not a valid {kind} document."
    entries, directory_bytes = declared
    if entries > MAX_ARCHIVE_ENTRIES or directory_bytes > MAX_CENTRAL_DIRECTORY_BYTES:
        return f"The {kind} document holds too many parts to be read."
    return None


def _ooxml_problem(ext: str, data: bytes) -> str | None:
    kind = ext.upper()
    problem = _directory_problem(kind, data)
    if problem:
        return problem
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            entries = package.infolist()
    except Exception:
        return f"The file is not a valid {kind} document."
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        return f"The {kind} document holds too many parts to be read."
    names = {entry.filename for entry in entries}
    if "[Content_Types].xml" not in names or OOXML_MAIN_PARTS[ext] not in names:
        return f"The file is not a valid {kind} document."
    expanded = sum(entry.file_size for entry in entries)
    if expanded > MAX_EXPANDED_BYTES or expanded > MAX_COMPRESSION_RATIO * max(len(data), 1):
        return f"The {kind} document expands to more data than can be read."
    return None


def inspection_problem(filename: str, data: bytes) -> str | None:
    """What decoding finds wrong with *data*: an image of another format or none, a false Office package."""
    ext = _extension(filename)
    if ext in IMAGE_FORMATS:
        return _image_problem(ext, data)
    if ext in OOXML_MAIN_PARTS:
        return _ooxml_problem(ext, data)
    return None
