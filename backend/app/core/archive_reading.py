# SPDX-License-Identifier: Apache-2.0

"""Reading an uploaded ZIP archive's members within a byte budget.

``ZipInfo.file_size`` is metadata the archive declares, so whoever built it sets
it, and ``ZipFile.read`` decompresses a member to its real length whatever it
declares: a 1.5 MB archive declaring ``file_size=10`` for a 1.5 GB member is held
whole before anything can refuse it. A cap on the declared sizes is a cheap
early refusal, never a bound.

``read_bounded`` reads through ``ZipFile.open`` at most one byte past the
smaller of the limit and the declared size: ``ZipFile`` returns no more than a
member declares and checks its CRC there, so an under-declared member is refused
as corrupt having decompressed only what it declared, and any other member is cut
one byte past the limit. The declared size only ever lowers the read, never lets
more through. ``ArchiveBudget`` spends one limit across every member an import
reads. Decompressing is CPU-bound and synchronous: an async caller runs it off
the event loop (``asyncio.to_thread``).
"""

from __future__ import annotations

import zipfile
import zlib


class ArchiveTooLargeError(ValueError):
    """A member expanding past what is left of its budget."""


def read_bounded(archive: zipfile.ZipFile, member: str | zipfile.ZipInfo, limit: int) -> bytes:
    """*member*'s bytes, or ``ArchiveTooLargeError`` once it expands past *limit*.

    ``zipfile.BadZipFile`` when it cannot be read as it declares: a CRC that does
    not match, a truncated or damaged stream, encryption, an unknown compression.
    """
    info = member if isinstance(member, zipfile.ZipInfo) else archive.getinfo(member)
    try:
        with archive.open(info) as handle:
            data = handle.read(min(info.file_size, limit) + 1)
    except (zlib.error, EOFError, RuntimeError, NotImplementedError) as exc:
        raise zipfile.BadZipFile(f"'{info.filename}' cannot be read: {exc}") from exc
    if len(data) > limit:
        raise ArchiveTooLargeError(f"'{info.filename}' expands past {limit:,} bytes")
    return data


class ArchiveBudget:
    """What an archive's members may expand to in all, spent as they are read."""

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def read(
        self, archive: zipfile.ZipFile, member: str | zipfile.ZipInfo, *, max_member: int | None = None
    ) -> bytes:
        """*member*'s bytes, counted against the budget; ``ArchiveTooLargeError`` past it,
        or past *max_member*, the most this one member may expand to."""
        limit = self.remaining if max_member is None else min(self.remaining, max_member)
        data = read_bounded(archive, member, limit)
        self.remaining -= len(data)
        return data
