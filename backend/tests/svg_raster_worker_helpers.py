"""Stand-ins for the SVG raster worker's render function (``test_svg_raster_bounds.py``).

They run in the spawned worker process, which imports them by name: nothing
here may need more than the standard library.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def hang(data: bytes, box: int, keep_native_size: bool, *_render_options: object) -> bytes | None:
    """Write the worker's pid to the file *data* names, then never finish."""
    Path(data.decode()).write_text(str(os.getpid()))
    time.sleep(600)
    return None


def slow_start() -> None:
    """A worker start as slow as a loaded host makes it."""
    time.sleep(2)
