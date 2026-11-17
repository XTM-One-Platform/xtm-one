# SPDX-License-Identifier: Apache-2.0

"""Whether this launch of the web server already migrated and seeded the database.

``start-uvicorn.sh`` exports a fresh ``XTM_ONE_LAUNCH_ID`` every time it runs,
and uvicorn's supervisor hands its environment to every worker it starts -
the ones it starts at boot and the ones it starts later to replace a worker
recycled after ``MAX_REQUESTS``. Workers of one launch therefore run the same
image with the same environment, so once one of them has applied the
migrations and the platform bootstrap, a worker started after that has
nothing left to apply: it only needs its own per-process state
(``_adopt_launch_bootstrap`` in ``main.py``). Workers that start together
(a new pod) each still pass the migration lock - the later one finds nothing
to apply - and the later one adopts under the bootstrap lock.

Without this, every recycled worker re-ran the whole one-time startup
(migrations under the advisory lock, the full built-in content sync) and
answered its share of the pod's traffic with ``503`` for its whole length,
while the pod stayed Ready on the probes its sibling answered.

The marker is an empty file named after the launch id in the temp directory,
which every worker of a pod shares and a new container does not. No launch
id (uvicorn started without the launcher, a local dev server) or an
unwritable temp directory means no marker: every worker runs the full
startup, as before.

The launch is recorded once the bootstrap returned, including when one of its
failure-isolated steps logged that it waits for the next startup (brand assets
on an object-store outage, a built-in dataset under evaluation): that step is
retried by the next launch, never by a recycled worker. A recycled worker is
the OLD code as soon as a newer version has booted next to it (a rolling
deploy), and re-running the migrations or the full built-in content sync from
there fails against the newer migration head or puts the older content back.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

LAUNCH_ID_ENV = "XTM_ONE_LAUNCH_ID"

# Exactly what the launcher writes (32 lowercase hex characters). Anything
# else - a hand-set value, a blank - is no launch id: the variable is the
# launcher's, never a setting.
_LAUNCH_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


def current_launch_id() -> str | None:
    """The launcher's id of this launch, ``None`` when it did not start this process."""
    launch_id = os.environ.get(LAUNCH_ID_ENV, "").strip()
    return launch_id if _LAUNCH_ID_PATTERN.fullmatch(launch_id) else None


def _marker_path() -> Path | None:
    launch_id = current_launch_id()
    if launch_id is None:
        return None
    return Path(tempfile.gettempdir()) / f"xtm-one-launch-{launch_id}.bootstrapped"


def launch_bootstrap_done() -> bool:
    """A worker of this launch already applied the migrations and the bootstrap."""
    path = _marker_path()
    return path is not None and path.is_file()


def mark_launch_bootstrap_done() -> None:
    """Record that this launch's migrations and bootstrap are applied. Never raises."""
    path = _marker_path()
    if path is None:
        return
    try:
        path.touch(exist_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not record the startup bootstrap for this launch (%s): a "
            "recycled web worker will re-run the migrations and the seeding",
            format_exception_message(exc),
        )
