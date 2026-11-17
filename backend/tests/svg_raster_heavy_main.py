"""A ``__main__`` that maps most of the SVG raster worker's address-space budget before the worker starts.

Run as a script (``test_video_studio_brand_mark_raster.py``): the raster worker
is spawned, so it re-imports this module as ``__mp_main__`` and maps the same
address space before its initializer caps it - what a render process's
``__main__`` does on a host with many CPUs (numpy's BLAS, a thread stack and a
buffer per CPU). Prints ``drawn <bytes>`` or ``refused <reason>``.
"""

from __future__ import annotations

import mmap
import sys

from app.services import svg_raster

#: Mapped, never touched: address space, no memory.
_HELD = mmap.mmap(
    -1,
    svg_raster.WORKER_ADDRESS_SPACE_BYTES - (64 << 20),
    flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
    prot=mmap.PROT_READ,
)


def main(path: str) -> int:
    import asyncio
    from pathlib import Path

    rendered = asyncio.run(svg_raster.render_svg_async(Path(path).read_bytes(), box=2048))
    sys.stdout.write((f"drawn {len(rendered.png)}" if rendered.png else f"refused {rendered.reason}") + "\n")
    svg_raster.shutdown_svg_raster_worker()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
