"""Building archives that lie about their members, and measuring what reading them holds."""

import io
import struct
import tracemalloc
import zipfile

_MIB = 2**20


def declare_size(raw: bytes, name: str, declared: int) -> bytes:
    """*raw*, an archive, with *name* declaring *declared* uncompressed bytes in its
    local header and in the central directory, whatever it holds."""
    out = bytearray(raw)
    for signature, name_at, size_at in ((b"PK\x03\x04", 30, 22), (b"PK\x01\x02", 46, 24)):
        at = out.find(signature)
        while at != -1 and out[at + name_at : at + name_at + len(name)] != name.encode():
            at = out.find(signature, at + 4)
        assert at != -1, f"{name} is not in the archive"
        struct.pack_into("<I", out, at + size_at, declared)
    return bytes(out)


def corrupt_member(raw: bytes, name: str, how: str) -> bytes:
    """*raw*, an archive, with member *name* made unreadable: its stream damaged
    (``"damaged stream"``), its compression unknown (``"unknown compression"``), or
    flagged encrypted (``"encrypted"``)."""
    out = bytearray(raw)
    info = zipfile.ZipFile(io.BytesIO(raw)).getinfo(name)
    local = info.header_offset
    central = out.find(b"PK\x01\x02")
    while bytes(
        out[central + 46 : central + 46 + struct.unpack_from("<H", out, central + 28)[0]]
    ).decode() != name:
        central = out.find(b"PK\x01\x02", central + 4)
    if how == "damaged stream":
        start = local + 30 + struct.unpack_from("<H", out, local + 26)[0] + struct.unpack_from("<H", out, local + 28)[0]
        span = min(16, info.compress_size)
        out[start : start + span] = b"\xff" * span
    elif how == "unknown compression":
        for at, offset in ((local, 8), (central, 10)):
            struct.pack_into("<H", out, at + offset, 99)
    else:  # encrypted
        for at, offset in ((local, 6), (central, 8)):
            struct.pack_into("<H", out, at + offset, struct.unpack_from("<H", out, at + offset)[0] | 1)
    return bytes(out)


def peak_mib(action) -> tuple[int, BaseException | None]:
    """The most memory *action* held at once, in MiB, and what it raised."""
    tracemalloc.start()
    raised = None
    try:
        action()
    except Exception as exc:
        raised = exc
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return peak // _MIB, raised


async def peak_mib_of(awaitable) -> tuple[int, BaseException | None]:
    """``peak_mib`` for a coroutine; a thread it runs work in is traced too."""
    tracemalloc.start()
    raised = None
    try:
        await awaitable
    except Exception as exc:
        raised = exc
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return peak // _MIB, raised
