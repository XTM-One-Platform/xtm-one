"""Stand-ins for the Nova matching worker's start (``test_nova_matching.py``).

They run in the spawned worker process, which imports them by name: nothing
here may need more than the standard library.
"""

from __future__ import annotations

import os
import time


def hanging_start() -> None:
    """A worker start that never finishes in time."""
    time.sleep(600)


def dying_start() -> None:
    """A worker that dies while it starts (a crash in an import, the OOM killer)."""
    os._exit(3)
